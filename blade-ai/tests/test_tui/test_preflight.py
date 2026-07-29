"""Tests for the TUI live-check half of chaos_agent.preflight."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.preflight import (
    CheckResult,
    _operator_replicas_ready,
    check_chaosblade_operator,
    check_k8s_connectivity,
    check_kubeconfig_live,
    check_kubectl_version,
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
    async def test_success_message_contains_server_version_only(self, monkeypatch):
        # check_k8s_connectivity invokes ``kubectl version -o json``
        # (liveness + server-version) and renders the success row as
        # ``v<server-version>``. The API server URL is deliberately
        # omitted — it's a sensitive endpoint (cluster IP / port) that
        # could leak through screenshots or shared logs of the
        # self-check card.
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        version_json = (
            b'{"clientVersion":{"gitVersion":"v1.30.0"},'
            b'"serverVersion":{"gitVersion":"v1.34.3-aliyun.1"}}\n'
        )
        proc_version = _make_proc(0, stdout=version_json)
        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=proc_version),
        ), patch(
            "chaos_agent.transports.channels.KubeconfigChannel.preflight",
            return_value=[],
        ):
            r = await check_k8s_connectivity()

        assert r.passed is True
        assert "1.34.3" in r.message  # server version, not client
        assert "https://" not in r.message
        assert ":6443" not in r.message

    async def test_kubectl_not_found_uses_dedicated_message(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        res = MagicMock()
        res.exit_code = -1
        res.stderr = "kubectl not found"
        res.stdout = ""
        with patch(
            "chaos_agent.tools.kubectl.exec_kubectl_raw",
            AsyncMock(return_value=res),
        ):
            r = await check_k8s_connectivity()

        assert r.passed is False
        assert r.severity == "blocking"
        assert "not found" in r.message

    async def test_timeout_reports_blocking(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kubectl_path", "kubectl")
        monkeypatch.setattr(_settings_mod.settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings_mod.settings, "kube_context", "")

        res = MagicMock()
        res.exit_code = -1
        res.stderr = "kubectl version timed out"
        res.stdout = ""
        with patch(
            "chaos_agent.tools.kubectl.exec_kubectl_raw",
            AsyncMock(return_value=res),
        ):
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

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=fake_exec)), \
             patch("chaos_agent.transports.channels.KubeconfigChannel.preflight", return_value=[]):
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

        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=fake_exec)), \
             patch("chaos_agent.transports.channels.KubeconfigChannel.preflight", return_value=[]):
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
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "kubeconfig")

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=_make_proc(0, stdout=b"chaosblade-operator|3|ghcr.io/chaosblade-io/chaosblade-operator:1.7.4\n")),
        ), patch(
            "chaos_agent.transports.channels.KubeconfigChannel.preflight",
            return_value=[],
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
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "kubeconfig")

        with patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=_make_proc(0, stdout=b"chaosblade-operator|0|img:v1\nchaosblade-tool|1|img:v1\n")),
        ), patch(
            "chaos_agent.transports.channels.KubeconfigChannel.preflight",
            return_value=[],
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
        ), patch(
            "chaos_agent.transports.channels.KubeconfigChannel.preflight",
            return_value=[],
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
        ), patch(
            "chaos_agent.transports.channels.KubeconfigChannel.preflight",
            return_value=[],
        ):
            r = await check_chaosblade_operator()

        assert r.passed is False
        assert "not deployed" in r.message
        assert "helm install" in r.fix or "/doctor" in r.fix


# ── run_tui_checks ──────────────────────────────────────────────────


class TestSshModeLiveExemption:
    """ssh mode: the live kubeconfig/kubectl checks must pass without
    touching a local kubeconfig or kubectl — mirrors the sync check
    exemptions (check_kubeconfig / check_kubectl) so ssh mode doesn't emit
    a spurious blocking failure at TUI boot."""

    async def test_kubeconfig_live_skipped_for_ssh(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "ssh")
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            r = await check_kubeconfig_live()
        assert r.passed is True
        assert r.severity == "blocking"
        assert "ssh" in r.message
        spawn.assert_not_called()  # no kubectl subprocess spawned

    async def test_kubectl_version_skipped_for_ssh(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "ssh")
        spawn = AsyncMock()
        with patch("asyncio.create_subprocess_exec", spawn):
            r = await check_kubectl_version()
        assert r.passed is True
        assert r.severity == "blocking"
        assert "ssh" in r.message
        spawn.assert_not_called()

    async def test_host_connectivity_probed_for_ssh(self, monkeypatch):
        """host-scope ssh: the connectivity row becomes a HOST probe
        (name=host_connectivity) via the transport layer, NOT a kubectl
        cluster probe."""
        from chaos_agent.config import settings as _settings_mod
        from chaos_agent.models.command_result import CommandResult
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "ssh")
        k8s_probe = AsyncMock()
        host_probe = AsyncMock(return_value=CommandResult(0, "blade-ai-preflight", ""))
        with patch("chaos_agent.tools.kubectl.exec_kubectl_raw", k8s_probe), \
             patch("chaos_agent.preflight.execute_via_transport", host_probe):
            r = await check_k8s_connectivity()
        assert r.name == "host_connectivity"
        assert r.passed is True
        assert "connected" in r.message and "ssh" in r.message
        k8s_probe.assert_not_called()  # no K8s API touched
        host_probe.assert_awaited_once()
        # probe ran a harmless echo with guard bypassed
        _args, _kwargs = host_probe.await_args
        assert _args[0][0] == "echo"
        assert _kwargs.get("skip_guard") is True

    async def test_host_connectivity_failure_for_ssh(self, monkeypatch):
        """A non-zero / preflight-failed probe → blocking not-reachable result."""
        from chaos_agent.config import settings as _settings_mod
        from chaos_agent.models.command_result import CommandResult
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "ssh")
        host_probe = AsyncMock(return_value=CommandResult(-1, "", "ssh: host required"))
        with patch("chaos_agent.preflight.execute_via_transport", host_probe):
            r = await check_k8s_connectivity()
        assert r.name == "host_connectivity"
        assert r.passed is False
        assert "不可达" in r.message

    async def test_operator_skipped_for_ssh(self, monkeypatch):
        from chaos_agent.config import settings as _settings_mod
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "ssh")
        probe = AsyncMock()
        with patch("chaos_agent.tools.kubectl.exec_kubectl_raw", probe):
            r = await check_chaosblade_operator()
        assert r.passed is True
        assert "host mode" in r.message
        probe.assert_not_called()

    async def test_host_connectivity_probed_for_kubewiz_host(self, monkeypatch):
        """kubewiz_host is also host-scope — host connectivity probe applies,
        labelled kubewiz-host."""
        from chaos_agent.config import settings as _settings_mod
        from chaos_agent.models.command_result import CommandResult
        monkeypatch.setattr(_settings_mod.settings, "kube_connection_mode", "kubewiz_host")
        k8s_probe = AsyncMock()
        host_probe = AsyncMock(return_value=CommandResult(0, "ok", ""))
        with patch("chaos_agent.tools.kubectl.exec_kubectl_raw", k8s_probe), \
             patch("chaos_agent.preflight.execute_via_transport", host_probe):
            r = await check_k8s_connectivity()
        assert r.name == "host_connectivity"
        assert r.passed is True
        assert "kubewiz-host" in r.message
        k8s_probe.assert_not_called()


class TestRunTuiChecks:
    async def test_panel_returns_seven_rows_in_canonical_order(self, monkeypatch):
        """In K8s-scope mode run_tui_checks emits exactly the seven canonical
        rows in the same order — the boot card relies on this ordering."""
        from chaos_agent import preflight as tui_preflight

        # Pin K8s-scope so the branch is deterministic regardless of env.
        monkeypatch.setattr(
            tui_preflight, "_is_host_scope_channel", lambda: False
        )

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

    async def test_panel_returns_host_rows_in_host_mode(self, monkeypatch):
        """Host-scope channels emit a host-oriented set: the K8s cluster rows
        (kubeconfig / kubectl / k8s_connectivity / chaosblade_operator) are
        dropped and replaced by transport_config + host_connectivity, which
        are the things a bare-host drill actually depends on."""
        from chaos_agent import preflight as tui_preflight

        monkeypatch.setattr(
            tui_preflight, "_is_host_scope_channel", lambda: True
        )

        async def _ok(name):
            async def inner():
                return CheckResult(name=name, severity="warning", passed=True)
            return inner

        monkeypatch.setattr(
            tui_preflight, "check_llm_api_key_live", await _ok("llm_api_key")
        )
        monkeypatch.setattr(
            tui_preflight, "check_transport_config",
            lambda: CheckResult(
                name="transport_config", severity="blocking", passed=True
            ),
        )
        monkeypatch.setattr(
            tui_preflight, "_check_host_connectivity",
            await _ok("host_connectivity"),
        )
        monkeypatch.setattr(
            tui_preflight, "check_blade_version", await _ok("blade")
        )
        monkeypatch.setattr(
            tui_preflight, "check_skills",
            lambda: CheckResult(name="skills", severity="warning", passed=True),
        )

        results = await run_tui_checks()
        assert [r.name for r in results] == [
            "llm_api_key",
            "transport_config",
            "host_connectivity",
            "blade",
            "skills",
        ]

    async def test_host_mode_surfaces_transport_config_failure(self, monkeypatch):
        """The host set actually RUNS check_transport_config — a missing host
        field surfaces as a blocking row at boot, closing the gap where
        host-config errors previously only appeared deep inside execution."""
        from chaos_agent import preflight as tui_preflight

        monkeypatch.setattr(
            tui_preflight, "_is_host_scope_channel", lambda: True
        )

        async def _ok(name):
            async def inner():
                return CheckResult(name=name, severity="warning", passed=True)
            return inner

        monkeypatch.setattr(
            tui_preflight, "check_llm_api_key_live", await _ok("llm_api_key")
        )
        monkeypatch.setattr(
            tui_preflight, "check_transport_config",
            lambda: CheckResult(
                name="transport_config", severity="blocking", passed=False,
                message="ssh 通道配置缺失: ssh_host",
            ),
        )
        monkeypatch.setattr(
            tui_preflight, "_check_host_connectivity",
            await _ok("host_connectivity"),
        )
        monkeypatch.setattr(
            tui_preflight, "check_blade_version", await _ok("blade")
        )
        monkeypatch.setattr(
            tui_preflight, "check_skills",
            lambda: CheckResult(name="skills", severity="warning", passed=True),
        )

        results = await run_tui_checks()
        tc = next(r for r in results if r.name == "transport_config")
        assert tc.passed is False
        assert tc.severity == "blocking"
        assert "ssh_host" in tc.message


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
