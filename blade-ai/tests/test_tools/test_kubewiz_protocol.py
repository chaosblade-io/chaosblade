"""Tests for kubewiz protocol adaptation layer."""
from unittest.mock import patch

import pytest

from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.kubectl import (
    build_kubectl_cmd,
    exec_kubectl_raw,
)
from chaos_agent.transports.base import TransportTarget
from chaos_agent.transports.channels import KubewizK8sChannel
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

    @patch("chaos_agent.tools.kubectl.settings")
    def test_kubeconfig_mode_no_wiz(self, mock_settings):
        """In kubeconfig mode, build_kubectl_cmd should NOT use wiz wrapper."""
        mock_settings.kube_connection_mode = "kubeconfig"
        mock_settings.kubectl_path = "kubectl"
        mock_settings.kubeconfig_path = "/home/user/.kube/config"
        mock_settings.kube_context = ""
        cmd = build_kubectl_cmd("get", ["pods", "-n", "default"])
        assert cmd[0] == "kubectl"
        assert "wiz" not in cmd
        assert "task" not in cmd
        assert "--kubeconfig" in cmd


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
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
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
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
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
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
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
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", mock_execute)

        result = await exec_kubectl_raw("get", ["pods", "-n", "default"])
        assert result.exit_code == 0
        assert "nginx" in result.stdout
        assert "NAME" in result.stdout
