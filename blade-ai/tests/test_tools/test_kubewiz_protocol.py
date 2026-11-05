"""Tests for kubewiz protocol adaptation layer."""
from unittest.mock import patch

import pytest

from chaos_agent.config.settings import blade_ai_context
from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.kubectl_cli import (
    _build_kubectl_global_args,
    build_kubectl_cmd,
    exec_kubectl_raw,
)
from chaos_agent.transports.base import TransportTarget
from chaos_agent.transports.channels import (
    KubeconfigChannel,
    KubewizHostChannel,
    KubewizK8sChannel,
    SSHChannel,
    strip_local_connection_flags,
)
from chaos_agent.transports.protocol import parse_wiz_output


class TestParseWizOutput:
    """Tests for parse_wiz_output() — shared protocol parser."""

    def test_success_with_output(self):
        result = CommandResult(exit_code=0, stdout="exit_code: 0\npod/nginx Running\n", stderr="")
        parsed = parse_wiz_output(result)
        assert parsed.exit_code == 0
        assert parsed.stdout == "pod/nginx Running\n"

    def test_failure_exit_code(self):
        result = CommandResult(exit_code=0, stdout="exit_code: 1\nerror msg", stderr="")
        parsed = parse_wiz_output(result)
        assert parsed.exit_code == 1
        assert parsed.stdout == "error msg"

    def test_wiz_self_failure_passthrough(self):
        result = CommandResult(exit_code=1, stdout="", stderr="timeout")
        parsed = parse_wiz_output(result)
        assert parsed.exit_code == 1
        assert parsed.stderr == "timeout"

    def test_missing_exit_code_prefix(self):
        result = CommandResult(exit_code=0, stdout="no prefix here", stderr="")
        parsed = parse_wiz_output(result)
        assert parsed.exit_code == 1
        assert "wiz protocol error" in parsed.stderr


class TestKubewizK8sChannelWrapCommand:
    """Tests for KubewizK8sChannel.wrap_command() shlex.quote behavior.

    build_kubectl_cmd now returns raw kubectl commands; quoting is done
    by the channel's wrap_command method.
    """

    def _make_target(self):
        return TransportTarget(
            scope="k8s",
            kubewiz_cluster_uuid="test-uuid",
            kubewiz_profile="test-profile",
        )

    def test_json_args_quoted(self):
        """JSON argument should be protected by shlex.quote in --command string."""
        channel = KubewizK8sChannel()
        target = self._make_target()
        raw_cmd = ["kubectl", "patch", "deployment/nginx", "-n", "default",
                   "-p", '{"spec":{"replicas":1}}']
        wrapped = channel.wrap_command(raw_cmd, target)
        command_str = wrapped[wrapped.index("--command") + 1]
        assert "'{\"spec\":{\"replicas\":1}}'" in command_str

    def test_jsonpath_args_quoted(self):
        """jsonpath argument with curly braces should be protected by shlex.quote."""
        channel = KubewizK8sChannel()
        target = self._make_target()
        raw_cmd = ["kubectl", "get", "pod/nginx", "-n", "default",
                   "-o", "jsonpath={.spec.replicas}"]
        wrapped = channel.wrap_command(raw_cmd, target)
        command_str = wrapped[wrapped.index("--command") + 1]
        assert "jsonpath={.spec.replicas}" in command_str or "'jsonpath={.spec.replicas}'" in command_str

    def test_args_with_spaces_quoted(self):
        """Arguments containing spaces should be properly quoted."""
        channel = KubewizK8sChannel()
        target = self._make_target()
        raw_cmd = ["kubectl", "get", "pods", "-l", "app=my service"]
        wrapped = channel.wrap_command(raw_cmd, target)
        command_str = wrapped[wrapped.index("--command") + 1]
        assert "'app=my service'" in command_str

    def test_plain_args_no_extra_quoting(self):
        """Plain arguments (no special chars) should be minimally quoted or unquoted."""
        channel = KubewizK8sChannel()
        target = self._make_target()
        raw_cmd = ["kubectl", "get", "pods", "-n", "default"]
        wrapped = channel.wrap_command(raw_cmd, target)
        command_str = wrapped[wrapped.index("--command") + 1]
        assert "kubectl" in command_str
        assert "get" in command_str
        assert "pods" in command_str
        assert "default" in command_str

    def test_kubewiz_cmd_structure(self):
        """Verify overall kubewiz command structure."""
        channel = KubewizK8sChannel()
        target = TransportTarget(
            scope="k8s",
            kubewiz_cluster_uuid="cluster-abc-123",
            kubewiz_profile="prod",
        )
        raw_cmd = ["kubectl", "get", "pods", "-n", "kube-system"]
        wrapped = channel.wrap_command(raw_cmd, target)
        # Settings.wiz_path is read at wrap time; just verify structure
        assert wrapped[1:3] == ["task", "exec"]
        assert "--command" in wrapped
        assert "--cluster-uuid" in wrapped
        assert "cluster-abc-123" in wrapped
        assert "--profile" in wrapped
        assert "prod" in wrapped

    @patch("chaos_agent.tools.kubectl_cli.settings")
    def test_kubeconfig_mode_no_wiz(self, mock_settings):
        """In kubeconfig mode, build_kubectl_cmd should NOT use wiz wrapper.

        The builder reads local connection identity (--kubeconfig/--context)
        from the resolved ``target`` — state > settings via
        ``resolve_transport_target`` — never bare from ``kubectl_cli.settings``.
        So the identity is supplied through an explicit target (matching how
        every real caller invokes it) instead of the patched module settings,
        which now only back ``kubectl_path``. This keeps the assertion
        independent of whatever the ambient process-global settings hold.
        """
        mock_settings.kubectl_path = "kubectl"
        target = TransportTarget(
            scope="k8s",
            kubeconfig="/home/user/.kube/config",
            kube_context="",
        )
        cmd = build_kubectl_cmd("get", ["pods", "-n", "default"], target=target)
        assert cmd[0] == "kubectl"
        assert "wiz" not in cmd
        assert "task" not in cmd
        assert "--kubeconfig" in cmd


class TestLocalConnectionFlagStripping:
    """--kubeconfig / --context are LOCAL connection-identity flags. The build
    side (``_build_kubectl_global_args``) is CHANNEL-AGNOSTIC and always emits
    them at the command head; the AUTHORITATIVE transport channel — resolved
    from the target, the same resolution that picks ``wrap_command`` — decides
    survival: remote channels (kubewiz_k8s / kubewiz_host / ssh) strip them, the
    local ``kubeconfig`` channel keeps them.

    This is the by-construction cure for the settings-vs-target divergence. The
    former fix gated emission on a SETTINGS-ONLY ``resolve_channel_name()``
    lookup, but the LLM ``kubectl`` tool (and every graph node) executes against
    ``resolve_transport_target(state)``; the SDK HTTP inject route stores a
    per-call ``kube_connection_mode`` on the graph state WITHOUT syncing it back
    to settings, so the two authorities diverged — Dir A leaked --kubeconfig
    into a remote payload, Dir B dropped it from a local subprocess. Stripping in
    the target-resolved channel makes build and execution incapable of
    disagreeing.

    Regression for the ai-testing-platform resilience-discovery bug: resource
    discovery over a KubeWiz cluster (SPE) returned ``available=false`` because
    the built command carried the operator's local ``--kubeconfig`` into the
    remote payload (``stat <local-path>: no such file or directory``).
    """

    # ── build side is channel-agnostic (no settings-only gate) ──────
    def test_build_emits_local_identity_even_under_remote_mode(self):
        """The builder no longer gates on a settings-only channel lookup: even
        under a remote mode it emits the local identity, leaving stripping to the
        authoritative channel. Gating here WAS the settings-vs-target bug."""
        with blade_ai_context(
            kube_connection_mode="kubewiz_k8s",
            kubewiz_cluster_uuid="uuid-1",
            kubewiz_profile="p",
            kubeconfig_path="/local/settings.yaml",
            kube_context="ctx",
        ):
            assert _build_kubectl_global_args() == [
                "--kubeconfig", "/local/settings.yaml", "--context", "ctx",
            ]
            # explicit param overrides the settings fallback
            assert _build_kubectl_global_args(kubeconfig="/explicit.yaml") == [
                "--kubeconfig", "/explicit.yaml", "--context", "ctx",
            ]

    # ── strip helper: precise, leading-run only ─────────────────────
    def test_strip_removes_leading_flags_space_and_equals_forms(self):
        assert strip_local_connection_flags(
            ["kubectl", "--kubeconfig", "/x", "--context", "c", "get", "pods"]
        ) == ["kubectl", "get", "pods"]
        assert strip_local_connection_flags(
            ["kubectl", "--kubeconfig=/x", "--context=c", "get", "pods"]
        ) == ["kubectl", "get", "pods"]

    def test_strip_stops_at_subcommand_preserving_exec_payload(self):
        """A --context past the ``--`` separator is exec payload (script text for
        the container shell), never a kubectl global flag — it must survive. The
        scan stops at the subcommand, so the payload region is never reached."""
        cmd = ["kubectl", "--kubeconfig", "/x", "exec", "pod", "--",
               "sh", "-c", "echo --context keep"]
        assert strip_local_connection_flags(cmd) == [
            "kubectl", "exec", "pod", "--", "sh", "-c", "echo --context keep",
        ]

    def test_strip_noop_for_non_kubectl_command(self):
        """A non-kubectl command's own --context must not be stripped."""
        cmd = ["somecli", "--context", "x", "do"]
        assert strip_local_connection_flags(cmd) == cmd

    def test_strip_noop_when_no_leading_flags(self):
        cmd = ["kubectl", "get", "pods"]
        assert strip_local_connection_flags(cmd) is cmd  # identity, no copy

    # ── remote channels strip in wrap_command ───────────────────────
    @pytest.mark.parametrize("channel,target", [
        (KubewizK8sChannel(),
         TransportTarget(scope="k8s", kubewiz_cluster_uuid="u", kubewiz_profile="p")),
        (KubewizHostChannel(),
         TransportTarget(scope="host", host_name="h", kubewiz_profile="p")),
        (SSHChannel(),
         TransportTarget(scope="host", ssh_host="h", ssh_user="u")),
    ])
    def test_remote_wrap_command_strips_local_flags(self, channel, target):
        raw = ["kubectl", "--kubeconfig", "/leak/local.yaml",
               "--context", "leak-ctx", "get", "pods"]
        blob = " ".join(channel.wrap_command(raw, target))
        assert "--kubeconfig" not in blob
        assert "/leak/local.yaml" not in blob
        assert "--context" not in blob
        assert "leak-ctx" not in blob
        # the semantic command still rides the payload
        assert "get" in blob and "pods" in blob

    # ── remote channels strip in display_command ────────────────────
    @pytest.mark.parametrize("channel,target", [
        (KubewizK8sChannel(),
         TransportTarget(scope="k8s", kubewiz_cluster_uuid="u", kubewiz_profile="p")),
        (KubewizHostChannel(),
         TransportTarget(scope="host", host_name="h", kubewiz_profile="p")),
        (SSHChannel(),
         TransportTarget(scope="host", ssh_host="h", ssh_user="u")),
    ])
    def test_remote_display_command_strips_local_flags(self, channel, target):
        raw = ["kubectl", "--kubeconfig", "/leak/local.yaml", "get", "pods"]
        shown = channel.display_command(raw, target)
        assert "--kubeconfig" not in shown
        assert "/leak/local.yaml" not in shown
        assert shown.startswith("kubectl get pods")

    # ── local channel keeps the flags ───────────────────────────────
    def test_local_channel_keeps_local_flags(self):
        ch = KubeconfigChannel()
        target = TransportTarget(scope="k8s", kubeconfig="/local/settings.yaml")
        raw = ["kubectl", "--kubeconfig", "/local/settings.yaml",
               "--context", "ctx", "get", "pods"]
        assert ch.wrap_command(raw, target) == raw  # passthrough, flags intact
        shown = ch.display_command(raw, target)
        assert "--kubeconfig" in shown and "/local/settings.yaml" in shown

    # ── divergence regression: the target-resolved channel wins ─────
    def test_dir_a_remote_target_strips_even_when_settings_local(self):
        """Dir A: settings say local (kubeconfig_path set) but the per-call target
        is remote. The old settings-only gate leaked --kubeconfig into the remote
        payload; the target-resolved channel now strips it."""
        with blade_ai_context(
            kube_connection_mode="kubeconfig",
            kubeconfig_path="/leak/local.yaml",
            kube_context="leak-ctx",
        ):
            cmd = build_kubectl_cmd("get", ["pods", "-n", "default"])
            assert "--kubeconfig" in cmd  # build is channel-agnostic
            remote_target = TransportTarget(
                scope="k8s", kubewiz_cluster_uuid="u", kubewiz_profile="p",
            )
            blob = " ".join(KubewizK8sChannel().wrap_command(cmd, remote_target))
            assert "--kubeconfig" not in blob
            assert "/leak/local.yaml" not in blob

    def test_dir_b_local_target_keeps_even_when_settings_remote(self):
        """Dir B: settings say remote but the per-call target is local. The old
        settings-only gate stripped --kubeconfig at build, so the local subprocess
        lost it; the target-resolved local channel now keeps it."""
        with blade_ai_context(
            kube_connection_mode="kubewiz_k8s",
            kubewiz_cluster_uuid="u",
            kubewiz_profile="p",
            kubeconfig_path="/local/settings.yaml",
        ):
            cmd = build_kubectl_cmd("get", ["pods", "-n", "default"])
            assert "--kubeconfig" in cmd  # build no longer gates on settings
            local_target = TransportTarget(
                scope="k8s", kubeconfig="/local/settings.yaml",
            )
            assert KubeconfigChannel().wrap_command(cmd, local_target) == cmd


class TestExecKubectlRaw:
    """Tests for exec_kubectl_raw() via transport layer.

    exec_kubectl_raw now delegates to execute_via_transport. These tests
    mock execute_via_transport at the kubectl module level to verify
    correct delegation and result handling.
    """

    @pytest.mark.asyncio
    async def test_success_returns_result(self, monkeypatch):
        """exec_kubectl_raw returns the CommandResult from execute_via_transport."""
        async def mock_execute(cmd, target, **kwargs):
            return CommandResult(0, "NAME    READY   STATUS\nnginx   1/1     Running\n", "", 10.0)

        import sys
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", mock_execute)

        result = await exec_kubectl_raw("get", ["pods", "-n", "default"])
        assert result.exit_code == 0
        assert "nginx" in result.stdout

    @pytest.mark.asyncio
    async def test_failure_returns_error_result(self, monkeypatch):
        """exec_kubectl_raw returns error result when execute_via_transport fails."""
        async def mock_execute(cmd, target, **kwargs):
            return CommandResult(1, "", "command failed", 10.0)

        import sys
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", mock_execute)

        result = await exec_kubectl_raw("get", ["pods"], timeout=5.0)
        assert result.exit_code == 1
        assert "failed" in result.stderr

    @pytest.mark.asyncio
    async def test_exception_returns_minus_one(self, monkeypatch):
        """exec_kubectl_raw returns exit_code=-1 on exception."""
        async def mock_execute(cmd, target, **kwargs):
            raise FileNotFoundError("kubectl")

        import sys
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", mock_execute)

        result = await exec_kubectl_raw("get", ["pods"])
        assert result.exit_code == -1
        assert "not found" in result.stderr.lower() or "kubectl" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_kubeconfig_mode_passthrough(self, monkeypatch):
        """In kubeconfig mode, exec_kubectl_raw still delegates to execute_via_transport."""
        async def mock_execute(cmd, target, **kwargs):
            return CommandResult(0, "NAME    READY   STATUS\nnginx   1/1     Running\n", "", 10.0)

        import sys
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", mock_execute)

        result = await exec_kubectl_raw("get", ["pods", "-n", "default"])
        assert result.exit_code == 0
        assert "nginx" in result.stdout
        assert "NAME" in result.stdout
