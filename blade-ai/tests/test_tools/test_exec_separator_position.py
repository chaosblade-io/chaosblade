"""R45 pins: the ``--`` POSITION model at the facts face (tools layer).

R44 fixed whether a separator exists (the separator-less ``POD COMMAND``
form). R45 fixes WHERE it sits and what the stretch before it is worth:

  1. pflag is value-FIRST — a ``--`` that arrives while a value-taking flag
     is hungry (``-c --`` / ``-n --`` / ``--container --``) is that flag's
     VALUE, never a boundary. The legacy raw slice (first standalone ``--``
     token wins) read those lines as "separator at N" and judged the tail,
     while every client still saw the separator-less form — whose command
     the DEPRECATED path RUNS (kubectl v1.11 ``Complete``:
     ``p.Command = argsIn[1:]``; v1.23 keeps the same branch behind its
     warning). ``exec_separator_shape`` walks with the shared flag table and
     answers "no true separator" for those lines, routing them into the R44
     walker's refusal.

  2. Ahead of a TRUE separator the stretch may hold exactly ONE positional —
     the entry. Extra positionals there are the R45 stray class: v1.11 exec
     prepends them to the command (``exec pod rm /data/x -- cat`` ran
     ``rm /data/x cat``), current exec silently drops them, and ``kubectl
     debug`` resolves EVERY positional as a separate target. An admitted
     verdict on the trailing command would sign off on a call whose real
     effect is client- and subcommand-dependent — refused, strays named.

Controls pin the no-over-refusal side: single entry plus flag values (glued
AND space-separated, ``--image ubuntu``), the live node-debug carrier shape
(``debug node/n1 -it --image=ubuntu -- sleep 60``) and the R44 verdicts.

  3. A ``--`` in a flag's VALUE slot followed by a LATER true separator
     (``-c -- -- cat /f``) is the R45-4 cascade, measured red on the
     pre-fix code (probe G): pflag eats the first ``--`` as the ``-c``
     VALUE and the second is the true separator, so every real client runs
     the payload — but the legacy raw slice stopped at the FIRST standalone
     ``--`` and handed the inner judge the text of a flag value
     (``' -- cat /f'``). A read-only probe was refused with a wrong reason
     while a mutating payload was ruled only by its shape.
     ``inner_raw_after_double_dash`` now maps the TRUE separator's token
     index (``exec_separator_index``) back to a raw offset, so the content
     view starts where the command really runs.

  4. The offset mapping cross-checks TWO locators (R45-5, measured red on
     the first single-locator fix): the INDEX mapping over the dequoted
     argv and the WALK mapping over the source-preserving tokens must
     agree on the same raw ``--``. Each is blind where the other sees —
     a quote-glued word BEFORE the boundary (``-c'x y'``) splits the
     source view into more words than the argv, so the index map can
     land on a value slot's ``--`` (``pod -c'x y' -c -- -- cat`` mapped
     to ``' -- cat'`` — the flag value's tail, not the command);
     quote-glued words AFTER the boundary (``awk --sour'{print > "f"}'``;
     R42 measured-allow) split only there and must not affect the
     boundary. Disagreement — or a boundary step that is not a literal
     ``--`` — refuses instead of guessing.
"""

import pytest

from chaos_agent.tools._readonly_facts import (
    exec_separator_index,
    exec_separator_shape,
    inner_raw_after_double_dash,
)
from chaos_agent.tools.readonly import kubectl_exec_rejection_reason as judge


class TestPreSeparatorExtrasRefused:
    """Positionals between the entry and a true ``--`` -> refused (R45)."""

    @pytest.mark.parametrize(
        "v_args",
        [
            # the v1.11 command-head shape, all-dash-free so pflag accepts it
            "kubectl exec drill-pod rm /data/x -- cat /etc/os-release",
            "kubectl exec drill-pod dd if=/dev/zero of=/data/x bs=1M count=1 -- cat /f",
            # plain ghost tokens
            "kubectl exec drill-pod ghost -- cat /f",
            "kubectl exec drill-pod -n default ghost -- cat /f",
            "kubectl exec drill-pod -c mycontainer ghost -- cat /f",
            # trailing separator: the ghost was never judged (pre-R45 admitted
            # as a bare entry while v1.11 would have RUN it)
            "kubectl exec drill-pod ghost --",
            # debug reads every pre-separator positional as a separate TARGET
            "kubectl debug drill-pod rogue-pod --image=busybox -- cat /f",
            "kubectl debug node/n1 node/n2 -it --image=ubuntu -- cat /f",
        ],
    )
    def test_refused(self, v_args):
        assert judge(v_args) is not None

    def test_reason_names_the_strays_and_the_fix_shape(self):
        reason = judge("kubectl exec drill-pod ghost -- cat /f")
        assert reason is not None
        assert "'ghost'" in reason
        assert "POD [flags] -- COMMAND" in reason

    def test_reason_caps_a_long_stray_list(self):
        reason = judge("kubectl exec drill-pod a b c d e f g h i j -- cat /f")
        assert reason is not None
        assert " ..." in reason


class TestValueSlotSeparatorIsNotASeparator:
    """``--`` consumed as a flag's value: no separator -> the R44 route."""

    @pytest.mark.parametrize(
        "v_args",
        [
            # pflag value-FIRST: ``-c``/``-n`` eat the ``--``, so the line is
            # the separator-less form whose command the deprecated path runs
            "kubectl exec drill-pod rm /data/x -c -- cat",
            "kubectl exec drill-pod rm /data/x -n -- cat",
            # a value-slot ``--`` AND a later true one: the extras before the
            # TRUE separator refuse (the v1.11 ``argsIn[1:]`` set included rm)
            "kubectl exec drill-pod rm /data/x -c -- ghost -- cat",
        ],
    )
    def test_refused(self, v_args):
        assert judge(v_args) is not None


class TestSingleEntryKept:
    """One entry plus flag values -> unchanged verdicts (no over-refusal)."""

    @pytest.mark.parametrize(
        "v_args",
        [
            "kubectl exec drill-pod -n default -- cat /f",
            "kubectl exec drill-pod -c mycontainer -- cat /f",
            "kubectl exec drill-pod -qc mycontainer -- cat /f",
            "kubectl exec drill-pod -- ls -l /f",
            "kubectl exec drill-pod -- sh -c 'chroot /host cat /etc/os-release'",
            # the live node-debug carrier shape: space-separated flag VALUES
            # must not read as strays
            "kubectl debug node/n1 -it --image=ubuntu -- sleep 60",
            "kubectl debug node/n1 -it --image ubuntu --profile sysadmin -- sleep 60",
            "kubectl debug drill-pod --image busybox --profile sysadmin -- cat /f",
            # R44 parity: bare entry, trailing separator, no-dash fixtures
            "kubectl exec drill-pod",
            "kubectl exec drill-pod --",
            "kubectl debug drill-pod --image=busybox",
            "kubectl exec drill-pod -qc mycontainer",
        ],
    )
    def test_admitted(self, v_args):
        assert judge(v_args) is None


class TestWalkerIsTheSingleSource:
    """``exec_separator_shape`` — the walk both faces ride."""

    def test_true_separator_splits_at_it(self):
        before, after = exec_separator_shape(["drill-pod", "-n", "default", "--", "cat"])
        assert before == ["drill-pod"]
        assert after == ["cat"]

    def test_value_slot_dash_is_not_a_separator(self):
        before, after = exec_separator_shape(["drill-pod", "rm", "-c", "--", "cat"])
        assert before == ["drill-pod", "rm", "cat"]
        assert after is None

    def test_no_separator_reports_none(self):
        before, after = exec_separator_shape(["drill-pod", "-it"])
        assert before == ["drill-pod"]
        assert after is None

    def test_kubectl_prefix_is_peeled(self):
        before, after = exec_separator_shape(
            ["kubectl", "-n", "ns", "exec", "drill-pod", "--", "cat", "/f"]
        )
        assert before == ["drill-pod"]
        assert after == ["cat", "/f"]

    def test_trailing_separator_has_empty_tail(self):
        before, after = exec_separator_shape(["drill-pod", "--"])
        assert before == ["drill-pod"]
        assert after == []


class TestValueSlotDashThenTrueSeparator:
    """R45-4: pflag eats the first ``--`` as the ``-c`` VALUE, the second is
    the TRUE separator — the content view must start there."""

    @pytest.mark.parametrize(
        "v_args",
        [
            "kubectl exec drill-pod -c -- -- rm -rf /data",
            "kubectl exec drill-pod -c -- -- chroot /host iptables -F",
            "kubectl exec drill-pod -c -- -- sh -c 'chroot /host iptables -F'",
        ],
    )
    def test_mutating_payload_refused(self, v_args):
        assert judge(v_args) is not None

    @pytest.mark.parametrize(
        "v_args",
        [
            # real clients run ``cat /f`` / ``ls /f`` — read-only probes
            "kubectl exec drill-pod -c -- -- cat /f",
            "kubectl exec drill-pod -n -- -- ls /f",
        ],
    )
    def test_readonly_payload_admitted(self, v_args):
        assert judge(v_args) is None

    def test_inner_slice_starts_at_the_true_separator(self):
        inner, error = inner_raw_after_double_dash(
            "kubectl exec drill-pod -c -- -- cat /f"
        )
        assert error is None
        assert inner is not None
        assert inner.strip() == "cat /f"


class TestSeparatorIndexIsTheLocator:
    """``exec_separator_index`` — the raw-offset locator content views ride."""

    def test_value_slot_dash_is_skipped(self):
        assert exec_separator_index(["drill-pod", "-c", "--", "--", "cat"]) == 3

    def test_kubectl_prefix_counts_from_zero(self):
        assert (
            exec_separator_index(["kubectl", "exec", "drill-pod", "-c", "--", "--", "cat"])
            == 5
        )

    def test_no_true_separator_reports_none(self):
        assert exec_separator_index(["drill-pod", "-c", "--", "cat"]) is None
        assert exec_separator_index(["drill-pod", "-it"]) is None

    def test_swallowed_only_line_has_no_inner_text(self):
        # No true separator -> the R44 separator-less form; the caller's
        # ``inner_raw is None`` branch owns the refusal.
        inner, error = inner_raw_after_double_dash("kubectl exec drill-pod -c -- cat")
        assert inner is None
        assert error is None


class TestBoundaryOffsetLocators:
    """R45-5: the two offset locators must agree on one raw ``--``."""

    @pytest.mark.parametrize(
        "v_args, expected",
        [
            # quote-glued AFTER the boundary: splits only there — the
            # boundary is already known, the slice stays exact (R42's
            # measured-allow shape).
            (
                "kubectl exec drill-pod -n default -- awk --sour'{print > \"/tmp/x\"}' /dev/null",
                "awk --sour'{print > \"/tmp/x\"}' /dev/null",
            ),
            ("kubectl exec pod -- awk 'a b'", "awk 'a b'"),
            ("pod -- cat 'x y'", "cat 'x y'"),
            # glued value kept whole: ``-c--`` is one flag word, the next
            # ``--`` is the boundary
            ("pod -c-- -- cat", "cat"),
        ],
    )
    def test_locators_agree_and_slice_is_exact(self, v_args, expected):
        inner, error = inner_raw_after_double_dash(v_args)
        assert error is None
        assert inner is not None
        assert inner.strip() == expected

    @pytest.mark.parametrize(
        "v_args",
        [
            # quote-glued BEFORE the boundary: the source view splits more
            # words than the argv — the index stops mapping; refuse.
            "pod -c'x y' -- cat",
            # ...and with a value slot in between, the index map alone
            # would land on the value slot's ``--`` — the walk locator
            # disagrees and the cross-check refuses.
            "pod -c'x y' -c -- -- cat",
        ],
    )
    def test_glue_before_boundary_refuses_not_guesses(self, v_args):
        inner, error = inner_raw_after_double_dash(v_args)
        assert inner is None
        assert error is not None and "failing closed" in error

    def test_r42_measured_allow_still_admitted(self):
        # Whole-face control: the R42 allow form survives the cross-check.
        assert judge(
            "kubectl exec drill-pod -n default -- awk --sour'{print > \"/tmp/x\"}' /dev/null"
        ) is None
