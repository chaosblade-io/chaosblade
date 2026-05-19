"""Tests for the TUI live-check half of chaos_agent.preflight."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.preflight import (
    CheckResult,
    _operator_replicas_ready,
    check_chaosblade_operator,
    check_k8s_connectivity,
    check_skills,
    needs_operator_install,
    run_tui_checks,
)


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """Build a mock asyncio subprocess that yields canned stdout/stderr."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _make_skill(parent: Path, name: str, *, with_md: bool = True) -> Path:
    sd = parent / name
    sd.mkdir()
    if with_md:
        (sd / "SKILL.md").write_text("---\nname: " + name + "\n---\nbody", encoding="utf-8")
    return sd


# ── check_skills ─────────────────────────────────────────────────────


class TestCheckSkills:
    def test_passes_with_one_level_skills(self, tmp_path, monkeypatch):
        # Skills directory exists and contains valid SKILL.md folders →
        # passed message shows the directory path (per the boot card
        # spec — operators want to see WHERE the skills came from, not
        # just how many).
        _make_skill(tmp_path, "alpha")
        _make_skill(tmp_path, "beta")
        monkeypatch.setattr("chaos_agent.preflight.get_skills_dir", lambda: tmp_path)
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "disabled_skills", [])

        r = check_skills()
        assert r.passed is True
        assert r.severity == "warning"
        assert str(tmp_path) in r.message or "~" in r.message

    def test_disabled_count_appears_in_message(self, tmp_path, monkeypatch):
        # When skills are disabled, append ``(N disabled)`` to the
        # path so the operator notices their disabled-list config is
        # actually filtering things out.
        _make_skill(tmp_path, "alpha")
        _make_skill(tmp_path, "beta")
        monkeypatch.setattr("chaos_agent.preflight.get_skills_dir", lambda: tmp_path)
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "disabled_skills", ["alpha"])

        r = check_skills()
        assert r.passed is True
        assert "1 disabled" in r.message

    def test_nested_skill_md_not_counted(self, tmp_path, monkeypatch):
        # alpha/SKILL.md is one level deep — counted.
        # group/inner/SKILL.md is two levels deep — must NOT be counted
        # to stay aligned with SkillRegistry.load_from_directory().
        # We assert that the check still PASSES (one valid skill) and
        # surfaces the directory; the exact count is no longer shown,
        # but ``test_empty_dir_reports_no_skills`` covers the
        # zero-counted regression on its own.
        _make_skill(tmp_path, "alpha")
        nested_parent = tmp_path / "group"
        nested_parent.mkdir()
        _make_skill(nested_parent, "inner")
        monkeypatch.setattr("chaos_agent.preflight.get_skills_dir", lambda: tmp_path)
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "disabled_skills", [])

        r = check_skills()
        assert r.passed is True
        assert str(tmp_path) in r.message or "~" in r.message

    def test_empty_dir_reports_no_skills(self, tmp_path, monkeypatch):
        monkeypatch.setattr("chaos_agent.preflight.get_skills_dir", lambda: tmp_path)
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "disabled_skills", [])

        r = check_skills()
        assert r.passed is False
        assert r.severity == "warning"
        assert "No skill files found" in r.message
        assert "blade-ai skills install" in r.fix

    def test_nonexistent_dir_reports_directory_not_found(self, tmp_path, monkeypatch):
        ghost = tmp_path / "does-not-exist"
        monkeypatch.setattr("chaos_agent.preflight.get_skills_dir", lambda: ghost)
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "disabled_skills", [])

        r = check_skills()
        assert r.passed is False
        assert "Skills directory not found" in r.message


# ── check_k8s_connectivity ──────────────────────────────────────────


class TestCheckK8sConnectivity:
    async def test_success_message_contains_server_version_and_url(self, monkeypatch):
        # check_k8s_connectivity invokes ``kubectl version -o json``
        # (liveness + server-version) followed by
        # ``kubectl config view --minify -o jsonpath=…server`` (local
        # parse of the active cluster's API URL). Success message
        # shows ``v<server-version> · <url>`` per spec.
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        version_json = (
            b'{"clientVersion":{"gitVersion":"v1.30.0"},'
            b'"serverVersion":{"gitVersion":"v1.34.3-aliyun.1"}}\n'
        )
        proc_version = _make_proc(0, stdout=version_json)
        proc_url = _make_proc(0, stdout=b"https://10.0.0.1:6443")
        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=[proc_version, proc_url]),
        ):
            r = await check_k8s_connectivity()

        assert r.passed is True
        assert "1.34.3" in r.message  # server version, not client
        assert "https://10.0.0.1:6443" in r.message

    async def test_kubectl_not_found_uses_dedicated_message(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError()),
        ):
            r = await check_k8s_connectivity()

        assert r.passed is False
        assert r.severity == "blocking"
        assert "kubectl not found" in r.message

    async def test_timeout_reports_blocking(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        async def _hang(*a, **kw):
            await asyncio.sleep(10)
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=_hang)
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with patch("chaos_agent.preflight._self_check_timeout", lambda: 0):
                r = await check_k8s_connectivity()

        assert r.passed is False
        assert "timed out" in r.message

    async def test_kubeconfig_tilde_is_expanded(self, monkeypatch, tmp_path):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "~/.kube/config")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        captured: list[tuple] = []

        async def fake_exec(*args, **kw):
            captured.append(args)
            return _make_proc(0, stdout=b"running at https://x\n")

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=fake_exec)):
            await check_k8s_connectivity()

        assert captured, "create_subprocess_exec was never invoked"
        first_call_args = captured[0]
        # base_cmd starts with kubectl, then --kubeconfig <expanded>, then "cluster-info"
        assert "--kubeconfig" in first_call_args
        kubeconfig_arg = first_call_args[first_call_args.index("--kubeconfig") + 1]
        assert "~" not in kubeconfig_arg
        assert kubeconfig_arg.endswith(".kube/config")

    async def test_kube_context_added_to_command(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "prod-cluster")

        captured: list[tuple] = []

        async def fake_exec(*args, **kw):
            captured.append(args)
            return _make_proc(0, stdout=b"running at https://x\n")

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=fake_exec)):
            await check_k8s_connectivity()

        first_call_args = captured[0]
        assert "--context" in first_call_args
        assert first_call_args[first_call_args.index("--context") + 1] == "prod-cluster"


# ── check_chaosblade_operator ───────────────────────────────────────


class TestOperatorReplicasReady:
    @pytest.mark.parametrize(
        "stdout, expected",
        [
            ("3", True),
            ("1 1 1", True),
            ("0", False),
            ("0 1", False),         # the regression case from the bug list
            ("1 0 1", False),
            ("", False),
            ("   ", False),
            ("abc", False),
        ],
    )
    def test_token_parsing(self, stdout, expected):
        assert _operator_replicas_ready(stdout) is expected


class TestCheckChaosbladeOperator:
    async def test_single_deployment_ready(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=_make_proc(0, stdout=b"3")),
        ):
            r = await check_chaosblade_operator()

        assert r.passed is True
        assert r.severity == "warning"

    async def test_partial_zero_replicas_fails(self, monkeypatch):
        """Regression: '0 1' was historically misclassified as ready."""
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=_make_proc(0, stdout=b"0 1")),
        ):
            r = await check_chaosblade_operator()

        assert r.passed is False
        assert "not ready" in r.message

    async def test_kubectl_not_found_does_not_suggest_operator_install(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(side_effect=FileNotFoundError()),
        ):
            r = await check_chaosblade_operator()

        assert r.passed is False
        assert "kubectl not found" in r.message
        assert "kubectl" in r.fix
        assert "ChaosBlade Operator" not in r.fix

    async def test_namespace_missing_suggests_install(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=_make_proc(1, stderr=b'namespaces "chaosblade" not found')),
        ):
            r = await check_chaosblade_operator()

        assert r.passed is False
        assert "not deployed" in r.message
        assert "helm install" in r.fix or "/doctor" in r.fix


# ── run_tui_checks ──────────────────────────────────────────────────


class TestRunTuiChecks:
    async def test_panel_returns_seven_rows_in_canonical_order(self, monkeypatch):
        """run_tui_checks must always emit exactly the seven canonical rows
        in the same order — the boot card relies on this ordering."""
        from chaos_agent import preflight as tui_preflight

        async def _ok(name):
            async def inner():
                return CheckResult(name=name, severity="warning", passed=True)
            return inner

        monkeypatch.setattr(
            tui_preflight, "check_llm_api_key_live", await _ok("llm_api_key")
        )
        monkeypatch.setattr(
            tui_preflight, "check_kubeconfig_live", await _ok("kubeconfig")
        )
        monkeypatch.setattr(
            tui_preflight, "check_kubectl_version", await _ok("kubectl")
        )
        monkeypatch.setattr(
            tui_preflight, "check_blade_version", await _ok("blade")
        )
        monkeypatch.setattr(
            tui_preflight, "check_skills",
            lambda: CheckResult(name="skills", severity="warning", passed=True),
        )
        monkeypatch.setattr(
            tui_preflight, "check_k8s_connectivity", await _ok("k8s_connectivity")
        )
        monkeypatch.setattr(
            tui_preflight, "check_chaosblade_operator", await _ok("chaosblade_operator")
        )

        results = await run_tui_checks()
        assert [r.name for r in results] == [
            "llm_api_key",
            "kubeconfig",
            "kubectl",
            "blade",
            "skills",
            "k8s_connectivity",
            "chaosblade_operator",
        ]

    async def test_check_exception_does_not_crash_panel(self, monkeypatch):
        """A single check raising must not abort the gather — it surfaces
        as a failed CheckResult under that check's canonical name."""
        from chaos_agent import preflight as tui_preflight

        async def _explode():
            raise RuntimeError("kaboom")
        monkeypatch.setattr(tui_preflight, "check_kubectl_version", _explode)

        # Stub the rest so we don't hit real kubectl / LLM.
        async def _make_pass(name):
            async def inner():
                return CheckResult(name=name, severity="warning", passed=True)
            return inner
        monkeypatch.setattr(
            tui_preflight, "check_llm_api_key_live", await _make_pass("llm_api_key")
        )
        monkeypatch.setattr(
            tui_preflight, "check_kubeconfig_live", await _make_pass("kubeconfig")
        )
        monkeypatch.setattr(
            tui_preflight, "check_blade_version", await _make_pass("blade")
        )
        monkeypatch.setattr(
            tui_preflight, "check_skills",
            lambda: CheckResult(name="skills", severity="warning", passed=True),
        )
        monkeypatch.setattr(
            tui_preflight, "check_k8s_connectivity",
            await _make_pass("k8s_connectivity"),
        )
        monkeypatch.setattr(
            tui_preflight, "check_chaosblade_operator",
            await _make_pass("chaosblade_operator"),
        )

        results = await run_tui_checks()
        # All seven rows present, kubectl row is the failed one.
        assert len(results) == 7
        kubectl_row = next(r for r in results if r.name == "kubectl")
        assert kubectl_row.passed is False
        assert "Check failed" in kubectl_row.message


# ── needs_operator_install ──────────────────────────────────────────


class TestNeedsOperatorInstall:
    def test_true_when_operator_failed(self):
        assert needs_operator_install([
            CheckResult(name="chaosblade_operator", severity="warning", passed=False),
        ]) is True

    def test_false_when_operator_passed(self):
        assert needs_operator_install([
            CheckResult(name="chaosblade_operator", severity="warning", passed=True),
        ]) is False

    def test_false_when_operator_absent(self):
        assert needs_operator_install([
            CheckResult(name="other", severity="blocking", passed=False),
        ]) is False


# ── renderer: title + sort + blocking short-circuit ─────────────────


class TestRenderer:
    def test_title_includes_blocking_and_warning_counts(self, captured_console):
        from chaos_agent.tui.renderers.preflight import _render_results
        results = [
            CheckResult(name="a", severity="blocking", passed=False, message="a-msg"),
            CheckResult(name="b", severity="warning", passed=False, message="b-msg"),
            CheckResult(name="c", severity="blocking", passed=True),
        ]
        _render_results(captured_console, results)
        out = captured_console._console.file.getvalue()
        assert "1/3" in out
        assert "1 阻塞" in out
        assert "1 警告" in out

    def test_rows_sorted_blocking_first(self, captured_console):
        from chaos_agent.tui.renderers.preflight import _render_results
        results = [
            CheckResult(name="zzz_pass", severity="warning", passed=True),
            CheckResult(name="aaa_warn", severity="warning", passed=False, message="warn-msg"),
            CheckResult(name="mmm_block", severity="blocking", passed=False, message="block-msg"),
        ]
        _render_results(captured_console, results)
        out = captured_console._console.file.getvalue()
        i_block = out.find("mmm_block")
        i_warn = out.find("aaa_warn")
        i_pass = out.find("zzz_pass")
        assert i_block != -1 and i_warn != -1 and i_pass != -1
        assert i_block < i_warn < i_pass

    async def test_run_and_render_short_circuits_on_blocking(self, captured_console, monkeypatch):
        """When a blocking check fails, do not prompt for operator install."""
        from chaos_agent.tui.renderers import preflight as renderer

        async def fake_run_tui_checks():
            return [
                CheckResult(name="k8s_connectivity", severity="blocking", passed=False, message="x"),
                CheckResult(name="chaosblade_operator", severity="warning", passed=False, message="y"),
            ]
        monkeypatch.setattr(renderer, "run_tui_checks", fake_run_tui_checks)

        # If the prompt path is reached, this AsyncMock would record a call;
        # the assertion below ensures it is NOT reached.
        prompt_session = MagicMock()
        prompt_session.prompt_async = AsyncMock(return_value="s")

        results, action = await renderer.run_and_render(captured_console, session=prompt_session)
        assert action == ""
        prompt_session.prompt_async.assert_not_called()
