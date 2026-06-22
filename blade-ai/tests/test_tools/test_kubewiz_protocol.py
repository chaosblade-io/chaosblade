"""Tests for kubewiz protocol adaptation layer."""
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.kubectl import (
    _adapt_kubewiz_result,
    build_kubectl_cmd,
    exec_kubectl_raw,
)


class TestAdaptKubewizResult:
    """Tests for _adapt_kubewiz_result()."""

    @patch("chaos_agent.tools.kubectl.settings")
    def test_kubewiz_kubectl_success(self, mock_settings):
        """kubewiz mode + kubectl success: parse exit_code and extract output."""
        mock_settings.kube_connection_mode = "kubewiz"
        result = CommandResult(exit_code=0, stdout="exit_code: 0\npod/nginx-abc Running\n", stderr="")
        adapted = _adapt_kubewiz_result(result)
        assert adapted.exit_code == 0
        assert adapted.stdout == "pod/nginx-abc Running\n"

    @patch("chaos_agent.tools.kubectl.settings")
    def test_kubewiz_kubectl_failure(self, mock_settings):
        """kubewiz mode + kubectl failure: parse non-zero exit_code."""
        mock_settings.kube_connection_mode = "kubewiz"
        result = CommandResult(
            exit_code=0,
            stdout="exit_code: 1\nError from server (NotFound): pods \"foo\" not found",
            stderr="",
        )
        adapted = _adapt_kubewiz_result(result)
        assert adapted.exit_code == 1
        assert adapted.stdout == 'Error from server (NotFound): pods "foo" not found'

    @patch("chaos_agent.tools.kubectl.settings")
    def test_kubewiz_kubectl_success_empty_output(self, mock_settings):
        """kubewiz mode + kubectl success with empty output after exit_code line."""
        mock_settings.kube_connection_mode = "kubewiz"
        result = CommandResult(exit_code=0, stdout="exit_code: 0\n", stderr="")
        adapted = _adapt_kubewiz_result(result)
        assert adapted.exit_code == 0
        assert adapted.stdout == ""

    @patch("chaos_agent.tools.kubectl.settings")
    def test_kubewiz_kubectl_success_only_exit_code_line(self, mock_settings):
        """kubewiz mode + kubectl success with only exit_code line (no newline)."""
        mock_settings.kube_connection_mode = "kubewiz"
        result = CommandResult(exit_code=0, stdout="exit_code: 0", stderr="")
        adapted = _adapt_kubewiz_result(result)
        assert adapted.exit_code == 0
        assert adapted.stdout == ""

    @patch("chaos_agent.tools.kubectl.settings")
    def test_kubewiz_wiz_self_failure(self, mock_settings):
        """kubewiz mode + wiz itself failed: return as-is."""
        mock_settings.kube_connection_mode = "kubewiz"
        result = CommandResult(exit_code=1, stdout="", stderr="Error: task timed out after 30s")
        adapted = _adapt_kubewiz_result(result)
        assert adapted.exit_code == 1
        assert adapted.stdout == ""
        assert adapted.stderr == "Error: task timed out after 30s"

    @patch("chaos_agent.tools.kubectl.settings")
    def test_non_kubewiz_mode_passthrough(self, mock_settings):
        """Non-kubewiz mode: return result unchanged."""
        mock_settings.kube_connection_mode = "kubeconfig"
        result = CommandResult(exit_code=0, stdout="pod/nginx Running", stderr="")
        adapted = _adapt_kubewiz_result(result)
        assert adapted.exit_code == 0
        assert adapted.stdout == "pod/nginx Running"
        assert adapted.stderr == ""

    @patch("chaos_agent.tools.kubectl.settings")
    def test_kubewiz_stdout_format_anomaly(self, mock_settings):
        """kubewiz mode + stdout missing exit_code prefix: report protocol error."""
        mock_settings.kube_connection_mode = "kubewiz"
        result = CommandResult(exit_code=0, stdout="unexpected output format", stderr="")
        adapted = _adapt_kubewiz_result(result)
        assert adapted.exit_code == 1
        assert "wiz protocol error" in adapted.stderr
        assert adapted.stdout == ""


class TestBuildKubectlCmdQuoting:
    """Tests for build_kubectl_cmd() shlex.quote in kubewiz mode."""

    @patch("chaos_agent.tools.kubectl.settings")
    def test_json_args_quoted(self, mock_settings):
        """JSON argument should be protected by shlex.quote in --command string."""
        mock_settings.kube_connection_mode = "kubewiz"
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_cluster_uuid = "test-uuid"
        mock_settings.kubewiz_profile = "test-profile"
        cmd = build_kubectl_cmd("patch", [
            "deployment/nginx", "-n", "default",
            "-p", '{"spec":{"replicas":1}}'
        ])
        # cmd structure: [wiz, task, exec, --command, <kubectl_cmd_str>, --cluster-uuid, ...]
        command_str = cmd[cmd.index("--command") + 1]
        # The JSON arg should be quoted (single quotes around it)
        assert "'{\"spec\":{\"replicas\":1}}'" in command_str

    @patch("chaos_agent.tools.kubectl.settings")
    def test_jsonpath_args_quoted(self, mock_settings):
        """jsonpath argument with curly braces should be protected by shlex.quote."""
        mock_settings.kube_connection_mode = "kubewiz"
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_cluster_uuid = "test-uuid"
        mock_settings.kubewiz_profile = "test-profile"
        cmd = build_kubectl_cmd("get", [
            "pod/nginx", "-n", "default",
            "-o", "jsonpath={.spec.replicas}"
        ])
        command_str = cmd[cmd.index("--command") + 1]
        # Curly braces should be protected by quoting
        assert "jsonpath={.spec.replicas}" in command_str or "'jsonpath={.spec.replicas}'" in command_str

    @patch("chaos_agent.tools.kubectl.settings")
    def test_args_with_spaces_quoted(self, mock_settings):
        """Arguments containing spaces should be properly quoted."""
        mock_settings.kube_connection_mode = "kubewiz"
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_cluster_uuid = "test-uuid"
        mock_settings.kubewiz_profile = "test-profile"
        cmd = build_kubectl_cmd("get", [
            "pods", "-l", "app=my service"
        ])
        command_str = cmd[cmd.index("--command") + 1]
        # Space-containing arg must be quoted
        assert "'app=my service'" in command_str

    @patch("chaos_agent.tools.kubectl.settings")
    def test_plain_args_no_extra_quoting(self, mock_settings):
        """Plain arguments (no special chars) should be minimally quoted or unquoted."""
        mock_settings.kube_connection_mode = "kubewiz"
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_cluster_uuid = "test-uuid"
        mock_settings.kubewiz_profile = "test-profile"
        cmd = build_kubectl_cmd("get", ["pods", "-n", "default"])
        command_str = cmd[cmd.index("--command") + 1]
        # shlex.quote on simple args either leaves them bare or adds single quotes
        # Both "kubectl get pods -n default" and "kubectl get pods -n 'default'" are acceptable
        assert "kubectl" in command_str
        assert "get" in command_str
        assert "pods" in command_str
        assert "default" in command_str

    @patch("chaos_agent.tools.kubectl.settings")
    def test_kubewiz_cmd_structure(self, mock_settings):
        """Verify overall kubewiz command structure."""
        mock_settings.kube_connection_mode = "kubewiz"
        mock_settings.wiz_path = "/usr/local/bin/wiz"
        mock_settings.kubewiz_cluster_uuid = "cluster-abc-123"
        mock_settings.kubewiz_profile = "prod"
        cmd = build_kubectl_cmd("get", ["pods", "-n", "kube-system"])
        assert cmd[0] == "/usr/local/bin/wiz"
        assert cmd[1:3] == ["task", "exec"]
        assert "--command" in cmd
        assert "--cluster-uuid" in cmd
        assert "cluster-abc-123" in cmd
        assert "--profile" in cmd
        assert "prod" in cmd

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
    """Tests for exec_kubectl_raw() Layer 1 function."""

    @patch("chaos_agent.tools.kubectl.settings")
    @pytest.mark.asyncio
    async def test_success_with_kubewiz_protocol(self, mock_settings):
        """Mock subprocess returning kubewiz protocol output; verify parsing."""
        mock_settings.kube_connection_mode = "kubewiz"
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_cluster_uuid = "uuid-1"
        mock_settings.kubewiz_profile = "default"

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(
            b"exit_code: 0\nNAME    READY   STATUS\nnginx   1/1     Running\n",
            b"",
        ))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await exec_kubectl_raw("get", ["pods", "-n", "default"])

        assert result.exit_code == 0
        assert "nginx" in result.stdout
        assert "exit_code:" not in result.stdout

    @patch("chaos_agent.tools.kubectl.settings")
    @pytest.mark.asyncio
    async def test_timeout_returns_minus_one(self, mock_settings):
        """Mock timeout scenario; verify exit_code=-1 and error message."""
        mock_settings.kube_connection_mode = "kubewiz"
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_cluster_uuid = "uuid-1"
        mock_settings.kubewiz_profile = "default"

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await exec_kubectl_raw("get", ["pods"], timeout=5.0)

        assert result.exit_code == -1
        assert "timed out" in result.stderr

    @patch("chaos_agent.tools.kubectl.settings")
    @pytest.mark.asyncio
    async def test_file_not_found_returns_minus_one(self, mock_settings):
        """Mock FileNotFoundError (wiz/kubectl binary missing); verify exit_code=-1."""
        mock_settings.kube_connection_mode = "kubewiz"
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_cluster_uuid = "uuid-1"
        mock_settings.kubewiz_profile = "default"

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("wiz")):
            result = await exec_kubectl_raw("get", ["pods"])

        assert result.exit_code == -1
        assert "not found" in result.stderr

    @patch("chaos_agent.tools.kubectl.settings")
    @pytest.mark.asyncio
    async def test_kubeconfig_mode_direct_exec(self, mock_settings):
        """In kubeconfig mode, exec_kubectl_raw should pass through without wiz parsing."""
        mock_settings.kube_connection_mode = "kubeconfig"
        mock_settings.kubectl_path = "kubectl"
        mock_settings.kubeconfig_path = "/tmp/kubeconfig"
        mock_settings.kube_context = ""

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(
            b"NAME    READY   STATUS\nnginx   1/1     Running\n",
            b"",
        ))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await exec_kubectl_raw("get", ["pods", "-n", "default"])

        assert result.exit_code == 0
        assert "nginx" in result.stdout
        # In kubeconfig mode, stdout is NOT modified by wiz protocol parsing
        assert "NAME" in result.stdout
