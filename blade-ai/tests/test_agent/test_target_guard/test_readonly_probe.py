"""Unit tests for ``is_readonly_host_probe`` (P-D).

Covers ``sh -c`` unwrapping and ``;`` / ``&&`` / ``||`` compound probes where
every segment must independently be a read-only probe, while pipes, redirects,
command substitution, and non-probe segments still fail closed.
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


class TestReadonlyHostProbeRejects:
    def test_pipe_fails_closed(self):
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'cat /etc/os-release | grep NAME'"
        )

    def test_redirect_fails_closed(self):
        assert not is_readonly_host_probe(
            "chroot /host sh -c 'echo x > /host/tmp/x'"
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

    def test_non_probe_binary_fails_closed(self):
        assert not is_readonly_host_probe("chroot /host sh -c 'rm -rf /tmp/x'")

    def test_empty_command(self):
        assert not is_readonly_host_probe("")
