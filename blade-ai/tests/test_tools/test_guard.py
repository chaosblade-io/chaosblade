"""Tests for ToolGuard command execution security."""

import json
import logging

import pytest

from chaos_agent.tools.guard import CommandResult, ToolGuard


class TestToolGuardCheck:
    """Test ToolGuard.check() command validation."""

    def setup_method(self):
        self.guard = ToolGuard()

    # ── Allowed commands ──────────────────────────────────────────────

    @pytest.mark.parametrize("cmd_first", ["blade", "df", "ping", "sleep"])
    def test_allowed_commands_pass(self, cmd_first):
        allowed, reason = self.guard.check([cmd_first, "arg1"])
        assert allowed is True
        assert reason == "OK"

    def test_kubectl_allowed_with_valid_subcommand(self):
        allowed, reason = self.guard.check(["kubectl", "get", "pods"])
        assert allowed is True
        assert reason == "OK"

    def test_blade_create_allowed(self):
        allowed, _ = self.guard.check(["blade", "create", "pod", "network", "delay"])
        assert allowed is True

    def test_kubectl_get_allowed(self):
        allowed, _ = self.guard.check(["kubectl", "get", "pods", "-n", "default"])
        assert allowed is True

    # ── Forbidden commands ─────────────────────────────────────────────

    @pytest.mark.parametrize("cmd_first", ["rm", "curl", "python", "bash", "sh", "wget", "chmod"])
    def test_forbidden_commands_rejected(self, cmd_first):
        allowed, reason = self.guard.check([cmd_first, "arg1"])
        assert allowed is False
        assert "not allowed" in reason

    def test_empty_command_rejected(self):
        allowed, reason = self.guard.check([])
        assert allowed is False
        assert "Empty" in reason

    # ── kubectl subcommand whitelist ───────────────────────────────────

    @pytest.mark.parametrize("subcmd", ["get", "describe", "delete", "exec", "logs", "top", "patch", "scale", "debug", "wait", "cordon", "uncordon", "taint"])
    def test_kubectl_allowed_subcommands(self, subcmd):
        allowed, _ = self.guard.check(["kubectl", subcmd, "pods"])
        assert allowed is True

    @pytest.mark.parametrize("subcmd", ["apply", "rollout", "edit", "create", "replace"])
    def test_kubectl_forbidden_subcommands(self, subcmd):
        allowed, reason = self.guard.check(["kubectl", subcmd, "something"])
        assert allowed is False
        assert "subcommand not allowed" in reason

    def test_kubectl_no_subcommand(self):
        """kubectl with no subcommand (just 'kubectl') is allowed since len(cmd)<=1."""
        allowed, _ = self.guard.check(["kubectl"])
        assert allowed is True

    def test_kubectl_with_kubeconfig_flag_passes(self):
        allowed, _ = self.guard.check([
            "kubectl", "--kubeconfig", "/my/kubeconfig", "get", "pods", "-n", "default",
        ])
        assert allowed is True

    def test_kubectl_with_context_flag_passes(self):
        allowed, _ = self.guard.check([
            "kubectl", "--context", "my-ctx", "get", "nodes",
        ])
        assert allowed is True

    def test_kubectl_with_kubeconfig_forbidden_subcommand(self):
        """Even with --kubeconfig, forbidden subcommands are still rejected."""
        allowed, reason = self.guard.check([
            "kubectl", "--kubeconfig", "/my/kubeconfig", "apply", "-f", "pod.yaml",
        ])
        assert allowed is False
        assert "subcommand not allowed" in reason

    def test_kubectl_with_only_flags_no_subcommand(self):
        """kubectl with only global flags and no subcommand should be allowed."""
        allowed, _ = self.guard.check(["kubectl", "--kubeconfig", "/my/kubeconfig"])
        assert allowed is True

    # ── Parameter blacklist patterns ───────────────────────────────────

    def test_rm_rf_blocked(self):
        allowed, reason = self.guard.check(["blade", "create", "rm -rf /"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_pipe_bash_blocked(self):
        allowed, reason = self.guard.check(["kubectl", "get", "pods", "| bash"])
        assert allowed is False

    def test_pipe_sh_blocked(self):
        allowed, reason = self.guard.check(["kubectl", "logs", "pod", "| sh"])
        assert allowed is False

    def test_redirect_dev_blocked(self):
        allowed, reason = self.guard.check(["blade", "create", ">", "/dev/null"])
        assert allowed is False

    def test_command_substitution_dollar_blocked(self):
        allowed, reason = self.guard.check(["blade", "$(", "whoami", ")"])
        assert allowed is False

    def test_backtick_blocked(self):
        allowed, reason = self.guard.check(["blade", "`whoami`"])
        assert allowed is False

    def test_semicolon_rm_blocked(self):
        allowed, reason = self.guard.check(["blade", "create", ";", "rm", "file"])
        assert allowed is False

    # ── Normal commands not triggering blacklist ───────────────────────

    def test_normal_blade_create_passes(self):
        allowed, _ = self.guard.check([
            "blade", "create", "pod", "network", "delay",
            "--time", "3000", "--interface", "eth0",
            "--names", "my-pod", "--namespace", "default",
        ])
        assert allowed is True

    def test_normal_kubectl_get_passes(self):
        allowed, _ = self.guard.check([
            "kubectl", "get", "pods", "-n", "default", "-o", "json",
        ])
        assert allowed is True

    # ── kubectl patch -p payload exclusion ─────────────────────────────

    def test_kubectl_patch_json_payload_with_dollar_paren_allowed(self):
        """kubectl patch -p value contains $( but it's JSON data, not shell injection."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "pvc", "my-pvc", "-n", "default",
            "-p", '{"spec":{"storageClassName":"$(whoami)"}}',
        ])
        assert allowed is True

    def test_kubectl_patch_json_payload_with_backticks_allowed(self):
        """kubectl patch -p value contains backticks but it's JSON data."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "deployment", "my-deploy", "-n", "default",
            "-p", '{"spec":{"template":{"`unused`":"value"}}}',
        ])
        assert allowed is True

    def test_kubectl_patch_equals_syntax_payload_excluded(self):
        """kubectl patch -p=VALUE syntax: payload value is excluded from check."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "pvc", "my-pvc", "-n", "default",
            "-p={'spec':{'storageClassName':'$(dangerous)'}}",
        ])
        assert allowed is True

    def test_kubectl_patch_long_flag_payload_excluded(self):
        """kubectl patch --patch=VALUE syntax: payload value is excluded."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "pvc", "my-pvc", "-n", "default",
            "--patch={'spec':{'storageClassName':'$(dangerous)'}}",
        ])
        assert allowed is True

    def test_kubectl_patch_dangerous_in_host_part_still_blocked(self):
        """Dangerous patterns outside -p value (in host part) are still blocked."""
        allowed, reason = self.guard.check([
            "kubectl", "patch", "pvc", "my-pvc", "-n", "default",
            "-p", '{"spec":{}}', "| bash",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_kubectl_patch_normal_payload_passes(self):
        """Normal kubectl patch with safe JSON payload passes."""
        allowed, _ = self.guard.check([
            "kubectl", "patch", "deployment", "my-deploy", "-n", "default",
            "-p", '{"spec":{"replicas":0}}',
        ])
        assert allowed is True

    # ── kubectl exec -- container command exclusion ─────────────────────

    def test_kubectl_exec_dangerous_in_container_allowed(self):
        """Dangerous patterns after -- (container command) are allowed."""
        allowed, _ = self.guard.check([
            "kubectl", "exec", "my-pod", "-n", "default", "--",
            "blade", "create", "k8s", "pod-cpu", "fullload",
        ])
        assert allowed is True

    def test_kubectl_exec_dangerous_before_separator_blocked(self):
        """Dangerous patterns before -- (host part) are still blocked."""
        allowed, reason = self.guard.check([
            "kubectl", "exec", "| bash", "--", "echo", "hi",
        ])
        assert allowed is False
        assert "Dangerous pattern" in reason


class TestToolGuardCustom:
    """Test ToolGuard with custom configuration."""

    def test_custom_allowed_commands(self):
        guard = ToolGuard(allowed_commands={"my-tool"})
        allowed, _ = guard.check(["my-tool", "arg"])
        assert allowed is True

    def test_custom_allowed_commands_override_default(self):
        guard = ToolGuard(allowed_commands={"my-tool"})
        allowed, reason = guard.check(["blade", "create"])
        assert allowed is False

    def test_custom_kubectl_subcommands(self):
        guard = ToolGuard(kubectl_subcommands={"get", "custom"})
        allowed, _ = guard.check(["kubectl", "custom", "arg"])
        assert allowed is True

    def test_custom_param_blacklist(self):
        guard = ToolGuard(param_blacklist=[r"DANGEROUS"])
        allowed, reason = guard.check(["blade", "DANGEROUS"])
        assert allowed is False


class TestToolGuardAuditLog:
    """Test ToolGuard.audit_log() output."""

    def test_audit_log_format(self, caplog):
        guard = ToolGuard()
        result = CommandResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=123.4,
        )
        with caplog.at_level(logging.INFO):
            guard.audit_log(["blade", "create"], result, task_id="task-123")

        assert len(caplog.records) == 1
        log_data = json.loads(caplog.records[0].message)
        assert log_data["task_id"] == "task-123"
        assert log_data["command"] == ["blade", "create"]
        assert log_data["exit_code"] == 0
        assert log_data["duration_ms"] == 123.4
        assert "timestamp" in log_data


class TestCommandResult:
    """Test CommandResult dataclass."""

    def test_default_duration(self):
        r = CommandResult(exit_code=0, stdout="ok", stderr="")
        assert r.duration_ms == 0.0

    def test_fields(self):
        r = CommandResult(exit_code=1, stdout="out", stderr="err", duration_ms=50.0)
        assert r.exit_code == 1
        assert r.stdout == "out"
        assert r.stderr == "err"
        assert r.duration_ms == 50.0
