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

    @pytest.mark.parametrize("subcmd", ["edit", "replace"])
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
            "kubectl", "--kubeconfig", "/my/kubeconfig", "edit", "deployment", "my-app",
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

    def test_kubectl_exec_solo_pipe_in_container_allowed(self):
        """Regression: solo ``|`` after ``--`` for exec must be allowed.

        Real LLM output (task-f8320b6ff844, msg #85): wanted to verify
        the chaosblade child process inside the chaosblade-tool DaemonSet
        with ``kubectl exec ... -- ps aux | grep mem``. Pre-fix the
        bare ``|`` token triggered SUSPICIOUS_SOLO_TOKENS and blocked the
        verification path. Post-fix the ``|`` lives in container_command
        which is exempt from the solo-token check.

        Note: under exec-form (shell=False) the ``|`` is forwarded as a
        literal argv to the container's ``ps``, not a host pipeline —
        no injection surface on the host. Real pipe semantics require
        ``-- sh -c "ps aux | grep mem"`` (already worked: the ``|``
        sits inside a single quoted token).
        """
        allowed, _ = self.guard.check([
            "kubectl", "exec", "chaosblade-tool-xxxx", "-n", "chaosblade",
            "--", "ps", "aux", "|", "grep", "mem",
        ])
        assert allowed is True

    @pytest.mark.parametrize("solo", [";", "|", "&", "||", "&&", ">", "<"])
    def test_kubectl_exec_all_solo_metachars_in_container_allowed(self, solo):
        """All SUSPICIOUS_SOLO_TOKENS are exempt inside container_command.

        Companion to the regression above — locks the rule "solo
        metachars after ``--`` are container-side, not host-side" for
        every token in the set so a future tightening that re-checks
        cmd-wide surfaces here, not just for ``|``.
        """
        allowed, _ = self.guard.check([
            "kubectl", "exec", "pod", "--", "sh", "-c", "true", solo, "echo", "x",
        ])
        assert allowed is True

    def test_kubectl_solo_pipe_outside_exec_still_blocked(self):
        """Solo ``|`` in host part (no ``--`` / non-exec subcommand)
        must still be rejected — the relaxation is scoped to
        container_command only."""
        allowed, reason = self.guard.check(["kubectl", "get", "pods", "|"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    def test_blade_solo_pipe_still_blocked(self):
        """blade has no ``--`` separator → all tokens are host-side →
        solo ``|`` must still be rejected."""
        allowed, reason = self.guard.check(["blade", "create", "|"])
        assert allowed is False
        assert "Dangerous pattern" in reason

    # ── E11 — AST-level parser edge cases ───────────────────────────────

    def test_kubectl_field_selector_with_special_chars(self):
        """E11: --field-selector value is a payload, not a shell command.
        Old host_part regex would have joined and could mis-detect; new
        parser puts it in data_payload_values so it's skipped."""
        allowed, _ = self.guard.check([
            "kubectl", "get", "pods",
            "--field-selector", "status.phase=Running",
        ])
        assert allowed is True

    def test_blade_subcommand_parsed_correctly(self):
        """E11: blade AST parser identifies subcommand + value flags
        without consuming positional args."""
        allowed, _ = self.guard.check([
            "blade", "create", "pod", "network", "delay",
            "--time", "3000", "--interface", "eth0",
            "--names", "my-pod", "--namespace", "default",
        ])
        assert allowed is True

    def test_unknown_kubectl_flag_treated_as_value_taking(self):
        """E11: unknown flag consumes next token (conservative
        fallback). Subcommand + remaining positional still parsed."""
        allowed, _ = self.guard.check([
            "kubectl", "get", "--made-up-future-flag", "value", "pods",
        ])
        # Should still pass: 'get' is allowed, no dangerous patterns
        assert allowed is True

    def test_blade_boolean_flag_h_does_not_consume_next_token(self):
        """E11 Gap A regression: blade -h is boolean, must not eat the
        next positional. If it did, parser would mis-locate 'pod' as
        the -h value and the subcommand check would still work, but
        a future check that depends on positional_args being correct
        would silently break."""
        from chaos_agent.tools.guard_parser import parse_command
        p = parse_command(["blade", "create", "-h", "pod"])
        assert p.subcommand == "create"
        assert "pod" in p.positional_args
        assert ("-h", None) in p.flags

    def test_kubectl_get_with_double_dash_treated_as_positional(self):
        """E11 Gap B regression: `--` outside exec/run/attach/debug
        MUST NOT split container_command. Otherwise a misplaced `--`
        would become a bypass channel for shell-pattern checks on
        anything that follows."""
        from chaos_agent.tools.guard_parser import parse_command
        p = parse_command(["kubectl", "get", "--", "pod"])
        assert p.subcommand == "get"
        assert p.container_command == ()
        # 'pod' must end up somewhere that host_relevant_tokens covers
        assert "pod" in p.host_relevant_tokens()

    def test_kubectl_global_boolean_flag_does_not_misidentify_subcommand(self):
        """E11 first-principles regression: the OLD inline parser
        (pre-E11) skipped any `-` token + the NEXT token together
        (assumed every flag was value-taking). That silently
        misidentified the subcommand whenever a global boolean flag
        appeared before it.

        Example: ``kubectl --insecure-skip-tls-verify get pods``
          - OLD parser: skip --insecure-skip-tls-verify + skip 'get'
            → subcommand="pods" → "pods" not in whitelist → REJECTED
            (false positive — get is a legal subcommand)
          - NEW parser: --insecure-skip-tls-verify is in
            KUBECTL_BOOLEAN_FLAGS → no consume → subcommand="get"
            → ALLOWED ✓

        This test was absent from the original 28 — none of them
        exercised a boolean global flag before the subcommand. Add
        it so a future revert of the AST parser would surface here
        instead of silently regressing real LLM-generated commands.
        """
        allowed, reason = self.guard.check([
            "kubectl", "--insecure-skip-tls-verify", "get", "pods",
        ])
        assert allowed is True, f"expected allow, got: {reason}"

    @pytest.mark.parametrize("boolean_flag", [
        "--insecure-skip-tls-verify",
        "--help",
        "-h",
    ])
    def test_kubectl_boolean_flag_before_subcommand(self, boolean_flag):
        """Parameterized companion to the regression above — every
        kubectl global boolean flag must allow subcommand to be
        identified correctly when placed before it."""
        cmd = ["kubectl", boolean_flag, "get", "pods"]
        allowed, _ = self.guard.check(cmd)
        assert allowed is True

    def test_container_command_with_dangerous_single_token_allowed(self):
        """E11 mutation-testing regression: the existing
        ``test_kubectl_exec_dangerous_in_container_allowed`` uses
        ``[blade, create, k8s, pod-cpu, fullload]`` as the container
        command — each token is harmless individually, so the test
        cannot distinguish between

          (a) host_relevant_tokens() correctly EXCLUDES container_command
          (b) host_relevant_tokens() includes container_command BUT
              the test cmd happens to have no matching token

        Both produce ALLOW. This test closes that gap by using a
        container command whose SINGLE token ``"rm -rf /"`` does match
        the ``rm\\s+-rf`` regex. If a future change accidentally
        promotes container_command into host_relevant_tokens, this
        test flips to FAIL.
        """
        allowed, _ = self.guard.check([
            "kubectl", "exec", "pod", "--",
            "sh", "-c", "rm -rf /",  # single token "rm -rf /" matches rm\s+-rf
        ])
        assert allowed is True

    def test_data_payload_with_dangerous_single_token_allowed(self):
        """E11 mutation-testing regression: similar gap for
        data_payload_values. The existing -p JSON payload tests use
        ``{"spec":{...}}`` which doesn't match any blacklist pattern
        in single-token form. This one uses a payload that DOES match
        the regex, so a future change that leaks payload values into
        host_relevant_tokens would surface here.
        """
        allowed, _ = self.guard.check([
            "kubectl", "patch", "pvc", "x", "-n", "default",
            "-p", '{"spec":{"x":"rm -rf /"}}',  # single token contains rm -rf
        ])
        assert allowed is True


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
