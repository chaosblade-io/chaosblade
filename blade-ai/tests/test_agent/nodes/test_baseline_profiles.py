"""Tests for the baseline capability-profile module.

Covers the three public helpers that decouple the baseline prompt / safety
layer from the connection channel:
  * ``profile_of`` (re-exported from transports) — channel → profile mapping.
  * ``build_baseline_system_prompt`` — universal core + per-profile fragment.
  * ``validate_command`` — per-profile read-only whitelist + shell-metachar
    rejection.
"""

import pytest

from chaos_agent.agent.nodes.baseline._baseline_profiles import (
    DIAG_BINARY_WHITELIST,
    build_baseline_system_prompt,
    validate_command,
)
from chaos_agent.transports import profile_of


# ---------------------------------------------------------------------------
# profile_of
# ---------------------------------------------------------------------------


class TestProfileOf:
    @pytest.mark.parametrize(
        "channel,expected",
        [
            ("kubeconfig", "k8s"),
            ("kubewiz_k8s", "k8s"),
            ("ssh", "host"),
            ("kubewiz_host", "host"),
        ],
    )
    def test_channel_maps_to_profile(self, channel, expected):
        assert profile_of(channel) == expected

    def test_unknown_channel_is_explicit(self):
        assert profile_of("mystery") == "unknown"


# ---------------------------------------------------------------------------
# build_baseline_system_prompt
# ---------------------------------------------------------------------------


class TestBuildBaselineSystemPrompt:
    def test_core_present_for_all_channels(self):
        """The universal mission core is channel-agnostic."""
        for channel in ("kubeconfig", "kubewiz_k8s", "ssh", "kubewiz_host"):
            prompt = build_baseline_system_prompt(channel)
            assert "Core Principle" in prompt
            assert "causation attribution" in prompt
            assert "Output Contract" in prompt

    def test_k8s_channels_get_kubectl_fragment(self):
        for channel in ("kubeconfig", "kubewiz_k8s"):
            prompt = build_baseline_system_prompt(channel)
            assert "Capability: Kubernetes" in prompt
            assert "Capability: Host shell diagnostics" not in prompt
            assert "debug_two_step" in prompt

    def test_host_channels_get_host_fragment(self):
        for channel in ("ssh", "kubewiz_host"):
            prompt = build_baseline_system_prompt(channel)
            assert "Capability: Host shell diagnostics" in prompt
            assert "Capability: Kubernetes" not in prompt

    def test_unknown_channel_is_fail_closed(self):
        prompt = build_baseline_system_prompt("mystery")
        assert "Capability: Unsupported environment" in prompt
        assert "Output an empty JSON list" in prompt


# ---------------------------------------------------------------------------
# validate_command — k8s profile
# ---------------------------------------------------------------------------


class TestValidateCommandK8s:
    @pytest.mark.parametrize(
        "command",
        [
            "kubectl get pods -n ns",
            "kubectl top node my-node",
            "kubectl describe node my-node",
            "kubectl exec pod-x -n ns -- df -h",
            "kubectl exec pod-x -n ns -- iostat -xd 1 3",
        ],
    )
    def test_allowed(self, command):
        assert validate_command(command, "k8s") is True

    @pytest.mark.parametrize(
        "command",
        [
            "kubectl delete pod x",          # non-whitelisted subcommand
            "kubectl debug node/x -- sh",    # debug is intentionally excluded
            "top -bn1",                       # not a kubectl command
            "kubectl exec pod-x -n ns -- rm -rf /",  # non-diagnostic exec
            "kubectl get pods | grep x",     # pipe
            "kubectl get pods > /tmp/x",     # redirect
            "kubectl get pods; rm -rf /",    # chain
            "kubectl get pods && whoami",    # chain
            "kubectl get $(whoami)",         # substitution
            "",                               # empty
        ],
    )
    def test_rejected(self, command):
        assert validate_command(command, "k8s") is False


# ---------------------------------------------------------------------------
# validate_command — host profile
# ---------------------------------------------------------------------------


class TestValidateCommandHost:
    @pytest.mark.parametrize(
        "command",
        [
            "top -bn1",
            "free -m",
            "df -h",
            "iostat -xd 1 2",
            "ss -s",
            "ip -s link",
            "ps aux",
            "uptime",
            "cat /proc/stat",
        ],
    )
    def test_allowed(self, command):
        assert validate_command(command, "host") is True

    @pytest.mark.parametrize(
        "command",
        [
            "kubectl get pods",              # kubectl not a host diagnostic
            "rm -rf /",                       # not whitelisted
            "ps aux | grep java",            # pipe
            "df -h > /tmp/out",              # redirect
            "top -bn1; rm -rf /",            # chain
            "uptime && whoami",              # chain
            "echo $(whoami)",                # substitution + non-whitelisted
            "",                               # empty
        ],
    )
    def test_rejected(self, command):
        assert validate_command(command, "host") is False


class TestValidateCommandSystemctl:
    """``systemctl`` is whitelisted only for read-only subcommands."""

    @pytest.mark.parametrize(
        "command",
        [
            "systemctl status nginx",
            "systemctl is-active nginx.service",
            "systemctl list-units --type=service --no-legend --plain",
            "systemctl show sshd",
            "dmesg",
            "nproc",
        ],
    )
    def test_readonly_allowed(self, command):
        assert validate_command(command, "host") is True

    @pytest.mark.parametrize(
        "command",
        [
            "systemctl stop nginx",
            "systemctl restart sshd",
            "systemctl start docker",
            "systemctl disable cron",
            "systemctl",  # bare — no read-only verb
        ],
    )
    def test_control_actions_rejected(self, command):
        assert validate_command(command, "host") is False


class TestValidateCommandUnknownProfile:
    def test_unknown_profile_fails_closed(self):
        assert validate_command("top -bn1", "jvm") is False


class TestAdvertisedDiagnosticsMatchEnforcement:
    """``DIAG_BINARY_WHITELIST`` is advertised in the capability prompt while
    ``tools.readonly`` is what actually validates. The two drifted once: the
    prompt listed 26 binaries whereas the validator accepted ~60, so genuinely
    usable diagnostics (``lsof`` / ``journalctl`` / ``sysctl`` / ``crictl``)
    were never offered to the LLM — capability without discoverability.

    Only the "no false advertising" direction can be asserted mechanically. The
    reverse (validator ⊆ advertised) is deliberately NOT asserted: the validator
    also accepts shell no-ops and network-egress tools that must stay out of a
    baseline prompt.
    """

    # A representative read-only invocation per advertised binary. Dual-use
    # entries need their inspection form, since a bare name may be rejected.
    _PROBE_ARGS = {
        "systemctl": "status kubelet",
        "sysctl": "-a",
        "journalctl": "-n 5",
        "crictl": "ps",
        "command": "-v iptables",
        "ip": "addr show",
        "mount": "-l",
        "grep": "-r pattern /etc/hosts",
        "find": "/etc -maxdepth 1",
        "stat": "/etc/hosts",
        "cat": "/proc/loadavg",
        "head": "-5 /proc/meminfo",
        "tail": "-5 /proc/meminfo",
        "wc": "-l /etc/hosts",
        "du": "-sh /tmp",
        "top": "-bn1",
        "iostat": "-xd 1 2",
        "pidof": "kubelet",
        "pgrep": "kubelet",
        "lsof": "-i",
        "blkid": "",
        "uname": "-a",
    }

    def test_every_advertised_binary_is_accepted_on_host(self):
        offenders = []
        for binary in sorted(DIAG_BINARY_WHITELIST):
            args = self._PROBE_ARGS.get(binary, "")
            command = f"{binary} {args}".strip()
            if not validate_command(command, "host"):
                offenders.append(command)
        assert not offenders, (
            f"advertised but rejected on the host profile: {offenders}"
        )

    def test_every_advertised_binary_is_accepted_after_kubectl_exec(self):
        offenders = []
        for binary in sorted(DIAG_BINARY_WHITELIST):
            args = self._PROBE_ARGS.get(binary, "")
            inner = f"{binary} {args}".strip()
            command = f"kubectl exec pod -n ns -- {inner}"
            if not validate_command(command, "k8s"):
                offenders.append(command)
        assert not offenders, (
            f"advertised but rejected after ``kubectl exec --``: {offenders}"
        )

    def test_diagnostics_enabled_by_the_readonly_judge_are_advertised(self):
        """Guards the specific gap that motivated this test: the read-only
        judge was widened for these, so they must also be discoverable."""
        for binary in ("lsof", "lsmod", "sysctl", "journalctl", "crictl"):
            assert binary in DIAG_BINARY_WHITELIST

    def test_prompt_fragments_list_the_advertised_set(self):
        """The fragments are built from the constant, so a future hand-edited
        literal list would silently re-open the drift."""
        for channel, binary in (("ssh", "lsof"), ("kubeconfig", "lsof")):
            prompt = build_baseline_system_prompt(channel)
            assert binary in prompt
