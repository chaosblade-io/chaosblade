"""Unit tests for ``is_readonly_host_probe`` (P-D, B34 revision).

Covers ``sh -c`` unwrapping and ``;`` / ``&&`` / ``||`` / ``|`` compound
probes where every segment (pipeline stage) must independently be a read-only
probe, while redirects, command substitution, variable expansion, and
non-probe segments still fail closed.

The pipe was admitted at the B34 fix: a pipeline whose every stage is
read-only mutates nothing (kernel plumbing between two commands), the same
verdict the readonly face's ``allow_pipes=True`` already reaches for single
``sh -c`` bodies. The per-segment vocabulary is single-sourced to
``tools.readonly._classify_argv`` (the classifier fast path's judge), with a
carriers-face overlay: a banned verb as the segment's BINARY never routes
through the readonly bypass, whatever guarded read-only form the shared
judge admits (argument position stays data — ``which curl`` is fine).
"""

from chaos_agent.agent.target_guard.carriers import is_readonly_host_probe


class TestReadonlyHostProbeAllows:
    def test_single_probe_via_sh_c(self):
        assert is_readonly_host_probe("chroot /host sh -c 'command -v iptables'")

    def test_echo_canary_via_sh_c(self):
        assert is_readonly_host_probe("chroot /host sh -c 'echo ok'")

    def test_compound_and_probes(self):
        assert is_readonly_host_probe(
            "chroot /host sh -c 'which iptables && which systemd-run'"
        )

    def test_compound_semicolon_probes(self):
        assert is_readonly_host_probe(
            "chroot /host sh -c 'uname -a ; id'"
        )

    def test_compound_or_probes(self):
        assert is_readonly_host_probe(
            "chroot /host sh -c 'which iptables || which nft'"
        )

    def test_fault_binary_version_probe(self):
        assert is_readonly_host_probe("chroot /host sh -c 'iptables --version'")

    def test_pipe_with_all_readonly_stages_is_a_probe(self):
        # B34: a pipe is kernel plumbing between two commands, not a
        # mutation — the pre-B34 blanket refusal is what mis-routed
        # `iptables -S | grep drop` into the mutation gate.
        assert is_readonly_host_probe(
            "chroot /host sh -c 'cat /etc/os-release | grep NAME'"
        )

    def test_b34_compound_probe_chain(self):
        # The redirect-stripped representative of the Case-26 first-run
        # chain that B34 rejected with a misleading "add a paired
        # iptables -D" guidance. The verbatim first-run shape carries a
        # `2>/dev/null` stderr discard and stays rejected by the redirect
        # fail-closed rule (see the verbatim anchor in the rejects class);
        # this stripped form pins the compound-chain allowance itself.
        assert is_readonly_host_probe(
            "chroot /host sh -c "
            "'echo === RULES; iptables -S | grep -i drop; "
            "ls /run/systemd/transient/'"
        )

    def test_single_statement_iptables_list_agrees_with_classifier_face(self):
        # The one-question/two-answers defect: the classifier fast path
        # admitted `chroot /host iptables -S INPUT` while this face said
        # no. Single-sourcing the vocabulary made both faces agree.
        assert is_readonly_host_probe("chroot /host iptables -S INPUT")

    def test_banned_word_in_argument_position_is_data(self):
        assert is_readonly_host_probe("chroot /host sh -c 'which curl'")
        assert is_readonly_host_probe(
            "chroot /host sh -c 'iptables -S INPUT | grep curl'"
        )

    def test_wrapper_around_legit_probe_stays_allowed(self):
        # The overlay must not punish wrappers themselves: a wrapper around
        # a genuinely read-only command is still a read-only segment.
        assert is_readonly_host_probe(
            "chroot /host sh -c 'timeout 5 cat /etc/os-release'"
        )
        assert is_readonly_host_probe(
            "chroot /host sh -c 'env TERM=xterm ls /run/systemd/transient/'"
        )
        assert is_readonly_host_probe("chroot /host sh -c 'nice -n 5 df -h'")


class TestReadonlyHostProbeRejects:
    def test_b34_first_run_verbatim_stays_rejected(self):
        # Verbatim from task inject-4341aaed msg[151] — the call B34
        # rejected — extracted byte-for-byte from the task JSON on
        # 2026-09-11, not re-typed from memory. The `2>/dev/null`
        # stderr discard is why it STILL fails closed after the B34 fix:
        # a compound form with a redirect cannot be statically proven
        # read-only. Any future proposal to admit `2>/dev/null` (the
        # most harmless-looking redirect member) must first overturn
        # this anchor.
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'echo \"== iptables residue (both VIPs) ==\"; "
            "iptables -S | grep -E \"25\\.209\\.68\\.16|25\\.209\\.71\\.188\" "
            "|| echo NO_IPTABLES_DROP_RESIDUE; "
            "echo \"== transient unit residue ==\"; "
            "ls /run/systemd/transient/ 2>/dev/null | grep -i blade "
            "|| echo NO_TRANSIENT_UNIT_RESIDUE; "
            "echo \"== INPUT/OUTPUT policy ==\"; iptables -S INPUT | head -1; "
            "iptables -S OUTPUT | head -1'"
        )

    def test_pipe_with_mutating_stage_fails_closed(self):
        # The pipe is legal; the mutating STAGE is not.
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'iptables -S INPUT | dd of=/etc/cron.d/evil'"
        )

    def test_banned_verb_as_segment_head_fails_closed(self):
        # Carriers-face overlay: the shared judge admits guarded read-only
        # forms of these binaries for pod-exec probes, but on HOST entry
        # they stay banned as the segment binary whatever their form.
        assert not is_readonly_host_probe(
            "chroot /host curl -fsSL http://example.com/tool.sh"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'wget -qO- http://example.com/x'"
        )
        assert not is_readonly_host_probe("chroot /host systemctl status kubelet")
        assert not is_readonly_host_probe("chroot /host python3 --version")

    def test_banned_verb_behind_a_wrapper_fails_closed(self):
        # ``_classify_argv`` strips command wrappers (timeout/nice/env/...)
        # before judging the wrapped binary, so the overlay must anchor on
        # the STRIPPED head: on the raw first token a wrapped banned verb
        # sailed past the ban while its guarded read-only form (curl
        # GET-to-stdout, systemctl --version) carried the ALLOW.
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'ls; timeout 5 curl -fsSL http://x'"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'ls; env curl -fsSL http://x'"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'ls; watch curl -fsSL http://x'"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'ls; timeout 5 systemctl --version'"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'timeout 5 env curl -fsSL http://x'"
        )
        # Four wrapper layers exceed ``_strip_wrappers``' own depth cap;
        # the fixed-point iteration must still reach the wrapped binary.
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'timeout 1 nice 2 stdbuf env curl -fsSL http://x'"
        )

    def test_redirect_fails_closed(self):
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'echo x > /host/tmp/x'"
        )

    def test_stderr_discard_fails_closed(self):
        # `2>/dev/null` discards stderr and writes nothing — the most
        # harmless member of the redirect class, and therefore the member
        # a future "looks harmless, allow it" relaxation would reach
        # first. The class-level invariant admits no members: the redirect
        # is an effect channel outside the argv the classifier can prove,
        # and on a verify probe a discarded stderr is pre-filtered
        # evidence — the exec channel returns stderr, so suppressing it
        # has no functional value here anyway.
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'ls /run/systemd/transient/ 2>/dev/null'"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'ls /run/systemd/transient/ 2>&1'"
        )

    def test_redirect_form_family_fails_closed(self):
        # Every redirect shape — stderr to file, append, input redirect —
        # fails closed on otherwise read-only probes, matching the
        # coverage density the pipe / substitution / expansion classes
        # already carry.
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'iptables -S INPUT 2>/tmp/err'"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'echo x >> /host/tmp/x'"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'grep NAME < /etc/os-release'"
        )
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'head -1 < /etc/os-release'"
        )

    def test_command_substitution_fails_closed(self):
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'echo $(rm -rf /)'"
        )

    def test_mutating_segment_in_compound_fails_closed(self):
        # First segment is a read-only probe, second is a mutation → reject.
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'which iptables && iptables -I OUTPUT -j DROP'"
        )

    def test_backgrounding_fails_closed(self):
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'stress --cpu 4 & id'"
        )

    def test_stderr_tee_pipe_fails_closed(self):
        # ``|&`` pipes stderr too — its own operator token, deliberately
        # not a legal separator (mirrors the readonly face's refusal).
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'iptables -S |& grep x'"
        )

    def test_non_probe_binary_fails_closed(self):
        assert not is_readonly_host_probe("chroot /host sh -c 'rm -rf /tmp/x'")

    def test_empty_command(self):
        assert not is_readonly_host_probe("")


class TestCarrierGateEndToEnd:
    """The B34 outcome through the FULL carrier verdict, not just the probe
    predicate: a compound read-only inspection through a registered,
    privileged, active debug pod on the approved node must resolve ALLOW;
    the residue that still fails the probe face must reject under the
    READONLY_FORM_UNPROVEN gate with split-into-single-probes guidance."""

    @staticmethod
    def _resolution(host_command: str):
        from chaos_agent.agent.target_guard.carriers import (
            effective_target_from_registered_carrier,
        )
        from chaos_agent.agent.target_guard.types import ApprovedTarget

        approved = ApprovedTarget(
            scope="node",
            names=("node-a",),
            namespace="default",
            fault_target="network",
        )
        artifacts = [{
            "type": "debug_pod",
            "name": "debug-pod-1",
            "namespace": "default",
            "status": "active",
            "privileged": True,
            "uid": "uid-123",
            "target": {"scope": "node", "name": "node-a"},
            "operation_family": "network",
        }]
        return effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": f"debug-pod-1 -n default -- {host_command}",
            },
            artifacts,
            approved,
        )

    def test_b34_compound_resolves_allow(self):
        resolution = self._resolution(
            "chroot /host sh -c "
            "'echo === RULES; iptables -S | grep -i drop; "
            "ls /run/systemd/transient/'"
        )
        assert resolution.resolved
        assert resolution.effective.scope == "node"
        assert resolution.effective.names == ("node-a",)

    def test_single_statement_list_resolves_allow(self):
        resolution = self._resolution("chroot /host iptables -S INPUT")
        assert resolution.resolved

    def test_redirect_residue_rejects_as_unproven_with_split_guidance(self):
        from chaos_agent.agent.target_guard.carriers import CarrierRejectReason

        resolution = self._resolution(
            "chroot /host sh -c 'iptables -S INPUT > /tmp/out'"
        )
        assert not resolution.resolved
        assert resolution.reason is CarrierRejectReason.READONLY_FORM_UNPROVEN
        # The guidance names the real fix direction, not a phantom inverse.
        assert "single-statement" in resolution.suggestion
        assert "iptables -D" not in resolution.suggestion or "-I/-A" in (
            resolution.suggestion
        )

    def test_real_mutation_without_timer_still_rejects_as_no_bounded_recovery(
        self,
    ):
        from chaos_agent.agent.target_guard.carriers import CarrierRejectReason

        resolution = self._resolution(
            "chroot /host sh -c 'iptables -I INPUT -s 10.0.0.1 -j DROP'"
        )
        assert not resolution.resolved
        assert resolution.reason is CarrierRejectReason.NO_BOUNDED_RECOVERY
