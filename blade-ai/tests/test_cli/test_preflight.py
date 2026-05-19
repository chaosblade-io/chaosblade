"""Tests for pre-flight self-check framework (preflight.py)."""

import os
from unittest.mock import MagicMock, patch

import pytest

from chaos_agent.preflight import (
    INJECT_CHECKS,
    RECOVER_CHECKS,
    LIST_CHECKS,
    CONFIRM_CHECKS,
    METRIC_CHECKS,
    CONFIG_CHECKS,
    VERSION_CHECKS,
    CheckResult,
    check_blade,
    check_kubeconfig,
    check_kubectl,
    check_llm_api_key,
    display,
    map_error,
    run,
)


# ── CheckResult model ──────────────────────────────────────────────────


class TestCheckResult:
    def test_passed_result_defaults(self):
        r = CheckResult(name="x", severity="blocking", passed=True)
        assert r.message == ""
        assert r.fix == ""

    def test_failed_result_with_details(self):
        r = CheckResult(
            name="llm_api_key",
            severity="blocking",
            passed=False,
            message="llm_api_key 未配置",
            fix="blade-ai config set llm_api_key <your-key>",
        )
        assert r.passed is False
        assert r.severity == "blocking"


# ── Atomic check functions ─────────────────────────────────────────────


class TestCheckLlmApiKey:
    def test_passes_when_key_set(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "llm_api_key", "sk-valid-key")
        result = check_llm_api_key()
        assert result.passed is True
        assert result.name == "llm_api_key"

    def test_fails_when_key_empty(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "llm_api_key", "")
        result = check_llm_api_key()
        assert result.passed is False
        assert result.severity == "blocking"
        assert "llm_api_key" in result.message
        assert "config set" in result.fix

    def test_fails_when_key_none(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "llm_api_key", None)
        result = check_llm_api_key()
        assert result.passed is False


class TestCheckKubeconfig:
    def test_passes_when_explicit_path_exists(self, monkeypatch, tmp_path):
        from chaos_agent.config import settings as _settings_mod

        kube_file = tmp_path / "kubeconfig"
        kube_file.write_text("apiVersion: v1", encoding="utf-8")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", str(kube_file))
        result = check_kubeconfig()
        assert result.passed is True

    def test_fails_when_explicit_path_not_found(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(
            _settings_mod.settings, "kubeconfig_path", "/nonexistent/kubeconfig"
        )
        result = check_kubeconfig()
        assert result.passed is False
        assert result.severity == "blocking"
        assert "不存在" in result.message

    def test_passes_with_default_kubeconfig(self, monkeypatch):
        """When no explicit kubeconfig_path, falls back to ~/.kube/config."""
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        default_path = os.path.expanduser("~/.kube/config")
        if os.path.isfile(default_path):
            result = check_kubeconfig()
            assert result.passed is True
        else:
            # No default on CI — just verify it returns a failed result
            result = check_kubeconfig()
            assert result.passed is False
            assert "默认" in result.message or "未配置" in result.message

    def test_fails_when_no_explicit_and_no_default(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        with patch("os.path.isfile", return_value=False):
            result = check_kubeconfig()
        assert result.passed is False
        assert "默认" in result.message or "未配置" in result.message

    def test_passes_when_path_has_tilde(self, monkeypatch, tmp_path):
        """Tilde in kubeconfig_path is expanded before checking the file."""
        from chaos_agent.config import settings as _settings_mod

        kube_file = tmp_path / "kubeconfig"
        kube_file.write_text("apiVersion: v1", encoding="utf-8")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "~/k_test_kc")

        real_expanduser = os.path.expanduser

        def fake_expanduser(path):
            if path == "~/k_test_kc":
                return str(kube_file)
            return real_expanduser(path)

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        result = check_kubeconfig()
        assert result.passed is True


class TestCheckKubectl:
    def test_passes_when_kubectl_found(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        with patch("shutil.which", return_value="/usr/bin/kubectl"):
            result = check_kubectl()
        assert result.passed is True

    def test_fails_when_kubectl_not_found(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        with patch("shutil.which", return_value=None):
            result = check_kubectl()
        assert result.passed is False
        assert result.severity == "blocking"
        assert "kubectl" in result.message

    def test_custom_kubectl_path(self, monkeypatch, tmp_path):
        from chaos_agent.config import settings as _settings_mod

        custom_path = str(tmp_path / "my-kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", custom_path)
        with patch("shutil.which", return_value=custom_path):
            result = check_kubectl()
        assert result.passed is True


class TestCheckBlade:
    def test_passes_when_blade_found(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(
            _settings_mod.settings, "blade_path", "/usr/local/bin/blade"
        )
        with patch("os.path.isfile", return_value=True):
            result = check_blade()
        assert result.passed is True
        assert result.severity == "warning"

    def test_warns_when_blade_not_found(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "blade_path", "")
        # _resolve_blade_path returns empty string when nothing found
        monkeypatch.setattr(
            _settings_mod.settings, "_resolve_blade_path", lambda: ""
        )
        with patch("shutil.which", return_value=None):
            result = check_blade()
        assert result.passed is False
        assert result.severity == "warning"
        assert "降级" in result.fix

    def test_blade_is_warning_not_blocking(self, monkeypatch):
        """blade check is always severity=warning, never blocking."""
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "blade_path", "")
        monkeypatch.setattr(
            _settings_mod.settings, "_resolve_blade_path", lambda: ""
        )
        with patch("shutil.which", return_value=None):
            result = check_blade()
        assert result.severity == "warning"


# ── Orchestration: run() ───────────────────────────────────────────────


class TestRun:
    def test_returns_all_passed(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "llm_api_key", "sk-test")
        results = run([check_llm_api_key])
        assert len(results) == 1
        assert results[0].passed is True

    def test_returns_mixed_results(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "llm_api_key", "sk-test")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "/nonexistent")
        results = run([check_llm_api_key, check_kubeconfig])
        assert len(results) == 2
        assert results[0].passed is True
        assert results[1].passed is False

    def test_handles_exception_in_check(self, monkeypatch):
        """If a check function raises, run() catches it and returns a warning result."""

        def _bad_check():
            raise RuntimeError("boom")

        results = run([_bad_check])
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].severity == "warning"
        assert "异常" in results[0].message

    def test_empty_checks_returns_empty(self):
        results = run([])
        assert results == []


# ── Orchestration: display() ───────────────────────────────────────────


class TestDisplay:
    def test_no_results_returns_false(self, capsys):
        assert display([]) is False
        # Nothing printed
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_all_passed_returns_false(self, capsys):
        results = [CheckResult(name="x", severity="blocking", passed=True)]
        assert display(results) is False

    def test_blocking_failure_returns_true(self, capsys):
        results = [
            CheckResult(
                name="llm_api_key",
                severity="blocking",
                passed=False,
                message="llm_api_key 未配置",
                fix="blade-ai config set llm_api_key <key>",
            )
        ]
        assert display(results) is True
        captured = capsys.readouterr()
        assert "❌" in captured.err
        assert "阻塞性" in captured.err

    def test_warning_failure_returns_false(self, capsys):
        """Warning-only failures should NOT trigger exit."""
        results = [
            CheckResult(
                name="blade",
                severity="warning",
                passed=False,
                message="blade 不可用",
                fix="建议安装 ChaosBlade",
            )
        ]
        assert display(results) is False
        captured = capsys.readouterr()
        assert "⚠️" in captured.err
        assert "警告" in captured.err

    def test_mixed_blocking_and_warning(self, capsys):
        results = [
            CheckResult(
                name="llm_api_key",
                severity="blocking",
                passed=False,
                message="llm_api_key 未配置",
                fix="config set",
            ),
            CheckResult(
                name="blade",
                severity="warning",
                passed=False,
                message="blade 不可用",
                fix="建议安装",
            ),
        ]
        assert display(results) is True
        captured = capsys.readouterr()
        assert "❌" in captured.err
        assert "⚠️" in captured.err
        assert "1 个阻塞性" in captured.err
        assert "1 个警告" in captured.err

    def test_output_goes_to_stderr(self, capsys):
        results = [
            CheckResult(
                name="x",
                severity="blocking",
                passed=False,
                message="fail",
                fix="fix it",
            )
        ]
        display(results)
        captured = capsys.readouterr()
        # Should be on stderr, not stdout
        assert captured.out == ""
        assert "❌" in captured.err


# ── Error mapping: map_error() ─────────────────────────────────────────


class TestMapError:
    def test_unmapped_exception_returns_none(self):
        result = map_error(ValueError("something"))
        assert result is None

    def test_openai_authentication_error(self):
        try:
            import openai

            exc = openai.AuthenticationError(
                message="Invalid API key",
                response=MagicMock(),
                body=None,
            )
            result = map_error(exc)
            assert result is not None
            assert result.name == "llm_api_key"
            assert result.severity == "blocking"
            assert "401" in result.message
        except ImportError:
            pytest.skip("openai not installed")

    def test_openai_api_connection_error(self):
        try:
            import openai

            exc = openai.APIConnectionError(request=MagicMock())
            result = map_error(exc)
            assert result is not None
            assert result.name == "api_base_url"
            assert result.severity == "blocking"
        except ImportError:
            pytest.skip("openai not installed")

    def test_openai_not_found_error(self):
        try:
            import openai

            exc = openai.NotFoundError(
                message="Not found",
                response=MagicMock(),
                body=None,
            )
            result = map_error(exc)
            assert result is not None
            assert "404" in result.message
        except ImportError:
            pytest.skip("openai not installed")

    def test_pattern_match_401(self):
        """Fallback pattern matching when openai types can't be imported."""
        exc = RuntimeError("Got 401 Unauthorized")
        result = map_error(exc)
        assert result is not None
        assert result.name == "llm_api_key"

    def test_pattern_match_invalid_api_key(self):
        exc = RuntimeError("invalid api key provided")
        result = map_error(exc)
        assert result is not None
        assert "llm_api_key" in result.name

    def test_cause_chain_unwrap(self):
        """map_error should unwrap __cause__ chain."""

        class InnerError(Exception):
            pass

        inner = RuntimeError("Got 401 Unauthorized")
        outer = ValueError("wrap")
        outer.__cause__ = inner
        result = map_error(outer)
        assert result is not None
        assert result.name == "llm_api_key"


# ── Check list declarations ────────────────────────────────────────────


class TestCheckLists:
    def test_inject_has_four_checks(self):
        assert len(INJECT_CHECKS) == 4
        assert check_llm_api_key in INJECT_CHECKS
        assert check_kubeconfig in INJECT_CHECKS
        assert check_kubectl in INJECT_CHECKS
        assert check_blade in INJECT_CHECKS

    def test_recover_matches_inject(self):
        assert RECOVER_CHECKS == INJECT_CHECKS

    def test_list_only_needs_llm(self):
        assert LIST_CHECKS == [check_llm_api_key]

    def test_confirm_only_needs_llm(self):
        assert CONFIRM_CHECKS == [check_llm_api_key]

    def test_metric_has_no_checks(self):
        assert METRIC_CHECKS == []

    def test_config_has_no_checks(self):
        assert CONFIG_CHECKS == []

    def test_version_has_no_checks(self):
        assert VERSION_CHECKS == []


# ── Integration: design doc verification scenario 1 ────────────────────
# "无 API key → 干净错误 + 修复指引"


class TestVerificationScenario1:
    """Design doc §7 scenario 1: No API key produces clean error with fix guidance."""

    def test_no_api_key_blocks_inject(self, monkeypatch, capsys):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "llm_api_key", "")
        results = run(INJECT_CHECKS)
        has_blocking = display(results)
        assert has_blocking is True

        captured = capsys.readouterr()
        assert "llm_api_key" in captured.err
        assert "config set" in captured.err

    def test_no_api_key_allows_config(self, monkeypatch, capsys):
        """config command should not trigger any preflight check."""
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "llm_api_key", "")
        results = run(CONFIG_CHECKS)
        has_blocking = display(results)
        assert has_blocking is False

    def test_no_api_key_allows_metric(self, monkeypatch, capsys):
        """metric command should not trigger any preflight check."""
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "llm_api_key", "")
        results = run(METRIC_CHECKS)
        has_blocking = display(results)
        assert has_blocking is False


# ── Integration: design doc verification scenario 4 ────────────────────
# "metric 不触发自检"


class TestVerificationScenario4:
    """Design doc §7 scenario 4: metric does not trigger self-check."""

    def test_metric_checks_empty(self):
        assert METRIC_CHECKS == []
        results = run(METRIC_CHECKS)
        assert results == []
        assert display(results) is False


# ── CLI ↔ TUI matrix contract ─────────────────────────────────────────


class TestCheckMatrices:
    """Guard against CLI / TUI preflight panels drifting apart.

    INJECT_CHECKS lists the four foundational concepts the CLI gates
    every injection run on (llm_api_key, kubeconfig, kubectl, blade).
    The TUI panel runs *live* equivalents for those concepts plus
    skills / k8s_connectivity / chaosblade_operator — assert by name
    so the matrices don't drift when the TUI replaces a sync check
    with a stronger async one.
    """

    def test_tui_panel_covers_all_cli_inject_concepts(self):
        # We can't run live checks here (no cluster, no LLM), so we
        # inspect the canonical ordering hardcoded in run_tui_checks.
        import inspect
        from chaos_agent.preflight import run_tui_checks

        src = inspect.getsource(run_tui_checks)
        # Each CLI check function name corresponds to a concept the
        # TUI panel must cover; resolve by stripping the "check_"
        # prefix and looking for the concept token in the source.
        for cli_check in INJECT_CHECKS:
            concept = cli_check.__name__.removeprefix("check_")
            assert concept in src, (
                f"INJECT_CHECKS ↔ TUI panel drift: {cli_check.__name__} "
                f"(concept '{concept}') not referenced in run_tui_checks"
            )

    def test_tui_panel_adds_skills(self):
        import inspect
        from chaos_agent.preflight import run_tui_checks

        assert "check_skills" in inspect.getsource(run_tui_checks)
