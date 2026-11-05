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
    check_transport_config,
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
            message="llm_api_key is not configured",
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
        assert "does not exist" in result.message

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
            assert "default" in result.message or "not configured" in result.message

    def test_fails_when_no_explicit_and_no_default(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        with patch("os.path.isfile", return_value=False):
            result = check_kubeconfig()
        assert result.passed is False
        assert "default" in result.message or "not configured" in result.message

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


class TestIsKubewizChannel:
    """Tests for _is_kubewiz_channel() — field inference + explicit override."""

    def test_primary_path_uuid_present(self, monkeypatch):
        """When kubewiz_cluster_uuid is set, field inference resolves to kubewiz_k8s."""
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "kubewiz_cluster_uuid", "uuid-1")
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "")
        from chaos_agent.preflight import _is_kubewiz_channel

        assert _is_kubewiz_channel() is True

    def test_override_forces_kubewiz_no_uuid(self, monkeypatch):
        """Explicit kube_connection_mode='kubewiz_k8s' selects the kubewiz
        channel even when kubewiz_cluster_uuid is not yet configured.

        This is the explicit channel override path — it replaces the old
        implicit fallback and lets preflight report missing kubewiz config
        before first use.
        """
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "kubewiz_cluster_uuid", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "kubewiz_k8s")
        from chaos_agent.preflight import _is_kubewiz_channel

        assert _is_kubewiz_channel() is True

    def test_returns_false_in_kubeconfig_mode(self, monkeypatch):
        """When neither uuid nor override indicates kubewiz, returns False."""
        from chaos_agent.config import settings as _settings_mod

        monkeypatch.setattr(_settings_mod.settings, "kubewiz_cluster_uuid", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "")
        from chaos_agent.preflight import _is_kubewiz_channel

        assert _is_kubewiz_channel() is False


class TestCheckTransportConfig:
    """check_transport_config validates host-scope channel fields (ssh / kubewiz_host)."""

    def _clear(self, m, s):
        for f in ("host_name", "ssh_host", "ssh_user", "ssh_key_path"):
            m.setattr(s, f, "", raising=False)
        m.setattr(s, "ssh_port", 22, raising=False)
        m.setattr(s, "kubewiz_cluster_uuid", "")
        m.setattr(s, "kubewiz_profile", "")
        m.setattr(s, "kube_connection_mode", "")

    def test_k8s_channel_not_covered_here(self, monkeypatch):
        """k8s-scope channels pass (validated by check_kubeconfig instead)."""
        from chaos_agent.config import settings as _sm
        from chaos_agent.preflight import check_transport_config

        self._clear(monkeypatch, _sm.settings)
        assert check_transport_config().passed is True

    def test_ssh_missing_host_fails(self, monkeypatch):
        """Override to ssh without ssh_host → blocking failure at boot."""
        from chaos_agent.config import settings as _sm
        from chaos_agent.preflight import check_transport_config

        self._clear(monkeypatch, _sm.settings)
        monkeypatch.setattr(_sm.settings, "kube_connection_mode", "ssh")
        r = check_transport_config()
        assert r.passed is False
        assert "ssh_host" in r.message

    def test_ssh_with_host_passes(self, monkeypatch):
        from chaos_agent.config import settings as _sm
        from chaos_agent.preflight import check_transport_config

        self._clear(monkeypatch, _sm.settings)
        monkeypatch.setattr(_sm.settings, "ssh_host", "10.0.0.2")
        assert check_transport_config().passed is True

    def test_kubewiz_host_missing_profile_fails(self, monkeypatch):
        from chaos_agent.config import settings as _sm
        from chaos_agent.preflight import check_transport_config

        self._clear(monkeypatch, _sm.settings)
        monkeypatch.setattr(_sm.settings, "host_name", "10.0.0.1")
        r = check_transport_config()
        assert r.passed is False
        assert "kubewiz_profile" in r.message

    def test_kubewiz_host_complete_passes(self, monkeypatch):
        from chaos_agent.config import settings as _sm
        from chaos_agent.preflight import check_transport_config

        self._clear(monkeypatch, _sm.settings)
        monkeypatch.setattr(_sm.settings, "host_name", "10.0.0.1")
        monkeypatch.setattr(_sm.settings, "kubewiz_profile", "526255")
        assert check_transport_config().passed is True

    def test_kubeconfig_skipped_for_host_channel(self, monkeypatch):
        """check_kubeconfig passes for host channels (no kubeconfig needed)."""
        from chaos_agent.config import settings as _sm
        from chaos_agent.preflight import check_kubeconfig

        self._clear(monkeypatch, _sm.settings)
        monkeypatch.setattr(_sm.settings, "ssh_host", "10.0.0.2")
        assert check_kubeconfig().passed is True

    def test_kubectl_skipped_for_ssh_channel(self, monkeypatch):
        """check_kubectl passes for ssh channel (executes on remote host)."""
        from chaos_agent.config import settings as _sm
        from chaos_agent.preflight import check_kubectl

        self._clear(monkeypatch, _sm.settings)
        monkeypatch.setattr(_sm.settings, "ssh_host", "10.0.0.2")
        assert check_kubectl().passed is True


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

        # A configured kubectl_path is a real file on disk. is_executable
        # validates it via os.path.isfile (NOT shutil.which, which mishandles
        # path-shaped cmds on Windows < 3.12), so the file must actually exist.
        custom = tmp_path / "my-kubectl"
        custom.write_text("#!/bin/sh\n")
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", str(custom))
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
        # check_blade is now pure detection (no download). It communicates
        # both the auto-download-on-first-inject path and the kubectl exec
        # fallback via message; the fix line points at manual override.
        assert "falls back" in result.message
        assert "blade_path" in result.fix

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
        assert "raised an error" in results[0].message

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
                message="llm_api_key is not configured",
                fix="blade-ai config set llm_api_key <key>",
            )
        ]
        assert display(results) is True
        captured = capsys.readouterr()
        assert "❌" in captured.err
        assert "blocking" in captured.err

    def test_warning_failure_returns_false(self, capsys):
        """Warning-only failures should NOT trigger exit."""
        results = [
            CheckResult(
                name="blade",
                severity="warning",
                passed=False,
                message="blade is unavailable",
                fix="Install ChaosBlade",
            )
        ]
        assert display(results) is False
        captured = capsys.readouterr()
        assert "⚠️" in captured.err
        assert "warning" in captured.err

    def test_mixed_blocking_and_warning(self, capsys):
        results = [
            CheckResult(
                name="llm_api_key",
                severity="blocking",
                passed=False,
                message="llm_api_key is not configured",
                fix="config set",
            ),
            CheckResult(
                name="blade",
                severity="warning",
                passed=False,
                message="blade is unavailable",
                fix="Install it",
            ),
        ]
        assert display(results) is True
        captured = capsys.readouterr()
        assert "❌" in captured.err
        assert "⚠️" in captured.err
        assert "1 blocking" in captured.err
        assert "1 warning" in captured.err

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
    def test_inject_has_five_checks(self):
        assert len(INJECT_CHECKS) == 5
        assert check_llm_api_key in INJECT_CHECKS
        assert check_kubeconfig in INJECT_CHECKS
        assert check_kubectl in INJECT_CHECKS
        assert check_transport_config in INJECT_CHECKS
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

    INJECT_CHECKS lists the foundational concepts the CLI gates every
    injection run on. The TUI panel runs *live* equivalents for the
    connectivity-oriented ones (llm_api_key, kubeconfig, kubectl, blade)
    plus skills / k8s_connectivity / chaosblade_operator — assert by name
    so the matrices don't drift when the TUI replaces a sync check
    with a stronger async one.

    ``transport_config`` is now covered too: ``run_tui_checks`` is
    mode-scoped, and its host-scope branch runs ``check_transport_config``
    so ssh/kubewiz_host users get host-field validation at boot. It stays
    out of the default k8s branch (where it is a no-op green row), but the
    concept token is present in the function source, so no exclusion is
    needed to keep the matrix in sync.
    """

    # All INJECT_CHECKS concepts now surface in run_tui_checks (k8s branch
    # or host branch). Nothing is excluded from the drift guard.
    _NON_LIVE_CONCEPTS: set[str] = set()

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
            if concept in self._NON_LIVE_CONCEPTS:
                continue
            assert concept in src, (
                f"INJECT_CHECKS ↔ TUI panel drift: {cli_check.__name__} "
                f"(concept '{concept}') not referenced in run_tui_checks"
            )

    def test_tui_panel_adds_skills(self):
        import inspect
        from chaos_agent.preflight import run_tui_checks

        assert "check_skills" in inspect.getsource(run_tui_checks)


# ── Scope registry (check registration) ────────────────────────────────


class TestScopeRegistry:
    """preflight-check-registration: the seven tier constants are load-time
    snapshots of the ``_CHECK_SCOPES`` registry; built-ins register through
    the same channel as external appends (single source of truth)."""

    def test_builtin_lists_match_historical_baseline(self):
        # Baseline captured before the registry refactor (order + members).
        assert [f.__name__ for f in INJECT_CHECKS] == [
            "check_llm_api_key",
            "check_kubeconfig",
            "check_kubectl",
            "check_transport_config",
            "check_blade",
        ]
        assert [f.__name__ for f in RECOVER_CHECKS] == [
            "check_llm_api_key",
            "check_kubeconfig",
            "check_kubectl",
            "check_transport_config",
            "check_blade",
        ]
        assert [f.__name__ for f in LIST_CHECKS] == ["check_llm_api_key"]
        assert [f.__name__ for f in CONFIRM_CHECKS] == ["check_llm_api_key"]
        assert METRIC_CHECKS == []
        assert CONFIG_CHECKS == []
        assert VERSION_CHECKS == []

    def test_registry_single_source_matches_snapshots(self):
        from chaos_agent import preflight as pf

        for scope, const in [
            ("inject", INJECT_CHECKS),
            ("recover", RECOVER_CHECKS),
            ("list", LIST_CHECKS),
            ("confirm", CONFIRM_CHECKS),
            ("metric", METRIC_CHECKS),
            ("config", CONFIG_CHECKS),
            ("version", VERSION_CHECKS),
        ]:
            assert pf.checks_for(scope) == const

    def test_external_append_lands_after_builtins(self):
        from chaos_agent import preflight as pf

        def fake_extra_check() -> CheckResult:
            return CheckResult(name="fake_extra", severity="blocking", passed=True)

        try:
            pf.register_check("inject", fake_extra_check)
            current = pf.checks_for("inject")
            assert current[-1] is fake_extra_check
            assert len(current) == len(INJECT_CHECKS) + 1
            # Built-in prefix unchanged (append-only ordering).
            assert current[: len(INJECT_CHECKS)] == INJECT_CHECKS
        finally:
            pf._CHECK_SCOPES["inject"].remove(fake_extra_check)

    def test_snapshot_constants_do_not_grow_after_append(self):
        from chaos_agent import preflight as pf

        def fake_extra_check() -> CheckResult:
            return CheckResult(name="fake_extra2", severity="blocking", passed=True)

        try:
            before = len(INJECT_CHECKS)
            pf.register_check("inject", fake_extra_check)
            # Snapshot constant keeps the load-time view...
            assert len(INJECT_CHECKS) == before
            # ...while the query path sees the append.
            assert pf.checks_for("inject")[-1] is fake_extra_check
        finally:
            pf._CHECK_SCOPES["inject"].remove(fake_extra_check)

    def test_unknown_scope_rejected(self):
        from chaos_agent import preflight as pf

        before = {s: list(v) for s, v in pf._CHECK_SCOPES.items()}
        with pytest.raises(ValueError) as exc:
            pf.register_check("deploy", lambda: None)
        assert "deploy" in str(exc.value)
        # The error lists the known set (fail-loud with guidance).
        assert "inject" in str(exc.value)
        # Registry state untouched by the rejected registration.
        assert {s: list(v) for s, v in pf._CHECK_SCOPES.items()} == before

    def test_checks_for_unknown_scope_rejected(self):
        from chaos_agent import preflight as pf

        with pytest.raises(ValueError):
            pf.checks_for("deploy")

    def test_append_into_empty_tier(self):
        from chaos_agent import preflight as pf

        def fake_metric_check() -> CheckResult:
            return CheckResult(name="fake_metric", severity="warning", passed=True)

        try:
            pf.register_check("metric", fake_metric_check)
            assert pf.checks_for("metric") == [fake_metric_check]
        finally:
            pf._CHECK_SCOPES["metric"].remove(fake_metric_check)


class TestCheckSkillsDedup:
    """``check_skills`` counts across the merged roots de-duplicated by the
    skill's frontmatter ``name`` — the SAME key ``SkillRegistry._scan_directory``
    uses — not by directory basename. Under ``dirname != frontmatter name``
    (e.g. a user override installed under a different dir name) basename
    de-dup would over-count vs what the registry actually loads, contradicting
    the docstring's shadowing claim. The count is internal (only its
    zero/non-zero branch is surfaced), so these tests lock the observable
    contract: the frontmatter-reading path doesn't crash and reports
    ``passed=True`` for a valid dir!=name shadowing pair, and tolerates a
    malformed SKILL.md via the lenient basename fallback.
    """

    @staticmethod
    def _mk(parent, dir_name, fm_name=None):
        d = parent / dir_name
        d.mkdir(parents=True, exist_ok=True)
        name = fm_name or dir_name
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test {name}\ncategory: test\n---\nbody",
            encoding="utf-8",
        )
        return d

    def test_dir_ne_name_shadowing_pair_passes(self, monkeypatch, tmp_path):
        from chaos_agent.preflight import check_skills

        user = tmp_path / "user"
        user.mkdir()
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        # User override: dir basename ("my-override") != frontmatter name it
        # declares ("k8s-chaos-skills"); shadows the built-in of that name.
        self._mk(user, "my-override", "k8s-chaos-skills")
        self._mk(builtin, "k8s-chaos-skills", "k8s-chaos-skills")
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skill_roots", lambda: [user, builtin]
        )
        assert check_skills().passed is True

    def test_malformed_frontmatter_does_not_crash(self, monkeypatch, tmp_path):
        from chaos_agent.preflight import check_skills

        user = tmp_path / "user"
        user.mkdir()
        bad = user / "broken"
        bad.mkdir()
        (bad / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skill_roots", lambda: [user]
        )
        # Lenient fallback: parse_frontmatter returns None → key falls back to
        # the basename, dir counted once, no AttributeError on ``None.get``.
        assert check_skills().passed is True

