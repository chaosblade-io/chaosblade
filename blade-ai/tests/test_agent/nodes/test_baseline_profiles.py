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
    validate_command_with_reason,
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


class TestNodeHostLevelChannelGuidance:
    """B45: the K8s fragment must model the execution environment and teach
    the four host-metric channels ordered by availability certainty.

    Case 43173315 (task inject-43173315, baseline phase): the fragment's
    NODE-metrics example taught ``kubectl exec {debug_pod} -- iostat ...`` —
    a diagnostic BARE in the container. The debug pod is a privileged but
    MINIMAL image (NPD carries no iostat), so six iostat variants failed
    deterministically and the retry loop burned three rounds re-running the
    same doomed shape on fresh pods (~60-90s + 4 pod lifecycles) before the
    model found /proc/diskstats on its own. Meanwhile the verify phase
    spontaneously used ``nsenter -t 1 ...`` and succeeded first try — the
    knowledge existed in the model, the prompt taught the wrong channel.
    """

    def _k8s_prompt(self) -> str:
        return build_baseline_system_prompt("kubeconfig")

    def test_environment_model_present(self):
        """The fragment states the environment fact the old example hid: a
        debug pod is a jump board into the node, not a diagnostic toolbox."""
        prompt = self._k8s_prompt()
        assert "jump board" in prompt
        assert "may carry no diagnostic binaries" in prompt

    def test_four_channels_ordered_by_availability(self):
        """API → /proc pseudo-files → nsenter host tools → bare-in-container,
        in that order (each successive channel is less certain to exist)."""
        prompt = self._k8s_prompt()
        markers = [
            "1. ``kubectl get/top/describe``",
            "2. kernel pseudo-files",
            "3. host tools via namespace entry",
            "4. a diagnostic binary BARE in the container",
        ]
        positions = [prompt.index(m) for m in markers]  # raises if any gone
        assert positions == sorted(positions)

    def test_every_advertised_example_passes_the_validator(self):
        """Invariant 1 (this module's own docstring): advertising a command
        the validator rejects burns baseline attempts on guaranteed
        failures. Both new exemplars must validate."""
        from chaos_agent.tools.pod_discovery import TOOL_POD_NAMESPACE

        for cmd in (
            f"kubectl exec {{debug_pod}} -n {TOOL_POD_NAMESPACE} "
            "-- cat /proc/diskstats",
            f"kubectl exec {{debug_pod}} -n {TOOL_POD_NAMESPACE} "
            "-- nsenter -t 1 -m -u -i -n -p -- iostat -xd 1 3",
            "kubectl top pod my-pod -n prod",
        ):
            assert validate_command(cmd, "k8s"), cmd

    def test_doomed_container_direct_exemplar_retired(self):
        """The NODE-metrics example block no longer teaches the bare
        in-container iostat form (the channel that failed deterministically
        on minimal images). The bare form stays LEGAL (layer 4) — it just
        stops being the exemplar the prompt leads with."""
        prompt = self._k8s_prompt()
        example_block = prompt[prompt.index("Examples:"):]
        assert "nsenter -t 1" in example_block
        assert "cat /proc/diskstats" in example_block
        assert "-- iostat" not in example_block.replace(
            "nsenter -t 1 -m -u -i -n -p -- iostat", ""
        )


# ---------------------------------------------------------------------------
# validate_command k8s exec target-zone form gate (Case #61 / W-61-1)
#
# The exec branch of validate_command_with_reason previously inspected only
# the INNER command after ``--`` (via kubectl_exec_rejection_reason whose
# judgement starts at the ``--`` boundary, prefix inert). Case #61 showed
# the LLM generalizing ``-l`` from get/top to exec, producing commands that
# failed at runtime with ``unknown shorthand flag: 'l'`` and were then
# laundered by retry into ``expected_absence`` (receipt said 7/7 while four
# container-internal dimensions were never measured). The form gate closes
# the prefix blind spot; these tests anchor the wiring.
# ---------------------------------------------------------------------------


class TestExecTargetFormGateWiring:
    """#61 four illegal commands are all rejected at validate_command with
    a reason that names the offending flag and points at the {target_pod}
    fix. Also anchors: (a) execute-side legal forms still pass; (b) Case #63
    dd conv=fsync rejection path is untouched."""

    # The exact four illegal commands from .b4tmp/c61_run1.log (LLM's ``-l``
    # generalization from get/top to exec). Verified verbatim via
    # ``grep -o "kubectl exec -l [^\"]*" .b4tmp/c61_run1.log``.
    C61_ILLEGAL_EXEC_COMMANDS = [
        "kubectl exec -l app=drill-perms-target -n default -- id",
        "kubectl exec -l app=drill-perms-target -n default -- ls -laR /tmp",
        "kubectl exec -l app=drill-perms-target -n default -- mount",
        "kubectl exec -l app=drill-perms-target -n default -- ps aux",
    ]

    @pytest.mark.parametrize("cmd", C61_ILLEGAL_EXEC_COMMANDS)
    def test_c61_illegal_commands_rejected_with_reason(self, cmd):
        reason = validate_command_with_reason(cmd, "k8s")
        assert reason is not None, f"#61 illegal command must be rejected: {cmd}"
        # Reason-fix pairing: names the offending selector flag AND points
        # to the {target_pod} placeholder as the fix.
        assert "selector" in reason.lower()
        assert "{target_pod}" in reason
        # Boolean view agrees with reason view (same single source).
        assert validate_command(cmd, "k8s") is False

    @pytest.mark.parametrize("cmd", [
        # #61 execute-side legal form: deploy/name prefix (used by inject
        # path itself).
        "kubectl exec deploy/drill-perms-target -n default -- id",
        # Literal pod name form.
        "kubectl exec drill-perms-target-6d4f8-x2k9p -n default -- cat /app/config.yaml",
        # Placeholder form (what the LLM should emit after this change).
        "kubectl exec {target_pod} -n default -- stat /app/config.yaml",
    ])
    def test_execute_side_legal_forms_pass(self, cmd):
        assert validate_command_with_reason(cmd, "k8s") is None
        assert validate_command(cmd, "k8s") is True

    def test_c63_dd_write_rejection_path_unchanged(self):
        """Case #63 (inject-2ee3bdc7): ``dd conv=fsync`` is a WRITE probe
        (baseline must be read-only) — the inner judge rejects it, and this
        rejection path must survive the form-gate wiring unchanged."""
        cmd = "kubectl exec some-pod -n default -- dd if=/dev/zero of=/tmp/x bs=1M count=1 conv=fsync"
        reason = validate_command_with_reason(cmd, "k8s")
        assert reason is not None
        # The rejection must come from the inner read-only judge, NOT from
        # the new form gate (form is fine: bare pod name + --).
        assert "selector" not in reason.lower()
        assert "{target_pod}" not in reason

    def test_form_gate_fires_before_inner_judge(self):
        """When BOTH gates would reject (selector flag + non-readonly inner),
        the form gate fires first (it is checked before the inner judge in
        the exec branch), so the reason mentions the form issue."""
        cmd = "kubectl exec -l app=foo -n default -- rm -rf /"
        reason = validate_command_with_reason(cmd, "k8s")
        assert reason is not None
        assert "selector" in reason.lower()  # form-gate reason
        # Not the inner judge's reason (which would mention rm or write).

    def test_missing_separator_still_uses_canonical_reason(self):
        """No ``--`` separator → existing canonical-form error (form gate
        abstains on this input, per its out-of-domain contract)."""
        cmd = "kubectl exec some-pod cat /etc/hosts"
        reason = validate_command_with_reason(cmd, "k8s")
        assert reason is not None
        assert "--" in reason
        assert "canonical" in reason.lower()
