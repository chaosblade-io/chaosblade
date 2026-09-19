"""Phase-3 face migrations: pinned facts verdicts (design doc 4.6/5.3).

Faces 3 (carriers probe), 4 (classifier escape peek + raw-inner judge) and
5 (baseline validate_command) run on the bashfacts judge — the ONLY engine
since the engine flip deleted the ``CHAOS_GUARD_READONLY_ENGINE`` switch and
the legacy chain with it. Every case below was measured under BOTH engines
before the flip and adjudicated (see
docs/design/bash-structural-guard-diff-adjudication.md §9); the no-drift
pins keep guarding against future drift.
"""

from chaos_agent.agent.nodes.baseline import _baseline_profiles
from chaos_agent.agent.target_guard import carriers, classifier
from chaos_agent.transports import PROFILE_HOST


def _probe(cmd: str) -> bool:
    return carriers.is_readonly_host_probe(cmd)


def _scope(v_args: str) -> str:
    # v_args follows the production convention (``_coerce_args_list`` /
    # ``_build_kubectl``): the subcommand is a separate field, so v_args
    # is the bare ``POD [flags] [--] COMMAND``. Spelling the subcommand
    # word INTO v_args composes ``kubectl exec exec mypod ...`` — a
    # different, invalid call the guard rightly refuses (R45 one-entry
    # rule).
    return classifier.infer_effective_target(
        "kubectl", {"subcommand": "exec", "v_args": v_args}
    ).scope


class TestCarriersFactsDivergences:
    """Face 3: the facts probe keeps the three carrier policies (separator
    segmentation / host-entry shape / parameter-expansion strictness) while
    closing the legacy tokenisation holes."""

    # --- quoted-literal fixes (legacy substring/token scans could not tell) ---
    def test_quoted_dollar_is_a_literal(self):
        assert _probe("chroot /host echo '$HOME'")

    def test_ansi_c_decoded_literal(self):
        # ``$'\x3b'`` decodes to a literal ``;`` — an echo ARGUMENT, not an
        # operator; the legacy scan saw the raw bytes and refused.
        assert _probe("chroot /host echo $'\\x3b' ls")

    def test_adjacent_semicolon_segments(self):
        # ``;`` glued to the previous word still segments (bash word rules);
        # the legacy separator split needed whitespace around it.
        assert _probe("chroot /host cat /etc/os-release; uname -a")

    # --- hole closures (legacy ALLOWED these) ---
    def test_head_peel_tail_no_longer_dropped(self):
        # Legacy peeled ``sh -c '...'`` and dropped the outer ``; rm -rf /``
        # tail; facts judges the whole script.
        assert not _probe("sh -c 'chroot /host ls' ; rm -rf /")

    def test_flag_before_dash_c_not_peeled(self):
        # Legacy index-peeled past ``--init-file /tmp/x``; the strict
        # three-word form refuses the shape.
        assert not _probe("chroot /host bash --init-file /tmp/x -c 'ls'")

    def test_flagged_shell_not_peeled(self):
        assert not _probe("chroot /host sh -x -c 'ls'")

    # --- strictness invariants (no divergence, pinned against drift) ---
    def test_bare_parameter_expansion_still_refused(self):
        assert not _probe("chroot /host echo $HOME")

    def test_process_substitution_still_refused(self):
        assert not _probe("chroot /host cat <(id)")


class TestClassifierFactsDivergences:
    """Face 4: the escape peek uses the loose ``unwrap_shell_invocation``
    (fail-open routing needs the peel to succeed), and the inner read-only
    verdict comes from the raw-inner facts judge."""

    def test_flagged_shell_peel_tightens_to_escape(self):
        # Legacy index-peeled ``sh -x -c`` and waved the probe through as
        # read-only; facts refuses to vouch for a flagged wrapper.
        # v_args is the POST-subcommand production shape (the tool contract:
        # ``POD [flags] -- INNER``) — a v_args that also spelled the
        # subcommand would double it in the coerced token stream
        # (``kubectl exec exec mypod ...``) and the R45 stray-positional
        # gate would refuse the shape before the inner judgment ran.
        assert _scope("mypod -- sh -x -c 'chroot /host id'") == classifier.SCOPE_ESCAPE

    def test_ansi_c_escape_head_recognised(self):
        # ``$'\x63hroot'`` decodes to ``chroot`` — legacy never saw the
        # escape primitive and attributed a plain pod scope.
        assert _scope("mypod -- $'\\x63hroot' /host id") == classifier.SCOPE_READONLY

    def test_usr_bin_sh_basename_peeled(self):
        # Legacy's peel table missed ``/usr/bin/sh``.
        assert _scope("mypod -- /usr/bin/sh -c 'chroot /host id'") == classifier.SCOPE_READONLY

    # --- no-drift pins ---
    def test_plain_readonly_unchanged(self):
        assert _scope("mypod -- df -h") == classifier.SCOPE_READONLY

    def test_mutating_escape_still_escape(self):
        v = "mypod -- sh -c 'chroot /host iptables -A INPUT'"
        assert _scope(v) == classifier.SCOPE_ESCAPE


class TestBaselineFactsDivergences:
    """Face 5: validate_command delegates to the readonly entries
    (facts-judged since the flip); the legacy ``_SHELL_METACHARS`` copy is
    gone."""

    def test_awk_quoted_program_allowed(self):
        # P1: ``>`` inside the quoted awk program is a comparison operator,
        # not a redirect (corpus C-01 class).
        assert _baseline_profiles.validate_command(
            "awk 'NR>1{print $1}' /etc/passwd", PROFILE_HOST
        )
