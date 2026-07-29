"""Tests for ChaosBlade CLI tool wrappers."""

from chaos_agent.tools.blade import _build_kubeconfig_arg, _get_host_blade_path, blade_create, blade_destroy, blade_help, blade_query_k8s, blade_status


class TestBuildKubeconfigArg:
    """Test _build_kubeconfig_arg helper."""

    def test_empty_by_default(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        assert _build_kubeconfig_arg() == []

    def test_explicit_kubeconfig(self):
        result = _build_kubeconfig_arg(kubeconfig="/my/kubeconfig")
        assert result == ["--kubeconfig", "/my/kubeconfig"]

    def test_settings_fallback(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "/from/settings")
        result = _build_kubeconfig_arg()
        assert result == ["--kubeconfig", "/from/settings"]

    def test_explicit_overrides_settings(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "/from/settings")
        result = _build_kubeconfig_arg(kubeconfig="/explicit")
        assert result == ["--kubeconfig", "/explicit"]

    def test_env_fallback(self, monkeypatch):
        # AliasChoices works at Settings init time, not runtime.
        # Verify Settings() reads KUBECONFIG, then monkeypatch the singleton.
        from chaos_agent.config.settings import Settings, settings as _settings
        monkeypatch.delenv("BLADE_AI_KUBECONFIG_PATH", raising=False)
        monkeypatch.setenv("KUBECONFIG", "/from/env")
        s = Settings()  # new instance proves AliasChoices works
        assert s.kubeconfig_path == "/from/env"
        monkeypatch.setattr(_settings, "kubeconfig_path", "/from/env")
        assert _build_kubeconfig_arg() == ["--kubeconfig", "/from/env"]


class TestBladeCreate:
    """Test blade_create tool function."""

    async def test_successful_create(self, mock_run_command):
        result = await blade_create.ainvoke({
            "scope": "pod",
            "target": "network",
            "action": "delay",
            "namespace": "",
            "names": "",
            "labels": "",
            "kubeconfig": "",
            "evict_count": "",
            "evict_percent": "",
            "flags": "--time 3000 --interface eth0",
        })
        assert "abc123" in result
        mock_run_command.assert_called_once()
        cmd = mock_run_command.call_args[0][0]
        assert cmd[0] == "blade"
        assert cmd[1] == "create"
        assert cmd[2] == "k8s"
        assert cmd[3] == "pod-network"
        assert cmd[4] == "delay"
        # flags are split and appended
        assert "--time" in cmd
        assert "3000" in cmd

    async def test_create_without_flags(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        await blade_create.ainvoke({
            "scope": "pod",
            "target": "pod",
            "action": "delete",
            "namespace": "",
            "names": "",
            "labels": "",
            "kubeconfig": "",
            "evict_count": "",
            "evict_percent": "",
            "flags": "",
        })
        cmd = mock_run_command.call_args[0][0]
        # No extra flags appended, but --timeout is auto-injected
        assert cmd[:5] == ["blade", "create", "k8s", "pod-pod", "delete"]
        assert "--timeout" in cmd

    async def test_create_normalizes_equals_timeout_without_adding_a_second_flag(
        self, mock_run_command,
    ):
        await blade_create.ainvoke({
            "scope": "node",
            "target": "network",
            "action": "drop",
            "flags": "--timeout=2700 --interface eth0",
        })

        cmd = mock_run_command.call_args[0][0]
        assert cmd.count("--timeout") == 1
        timeout_idx = cmd.index("--timeout")
        assert cmd[timeout_idx + 1] == "2700"

    async def test_create_deduplicates_timeout_flags_with_last_value(self, mock_run_command):
        await blade_create.ainvoke({
            "scope": "node",
            "target": "network",
            "action": "drop",
            "flags": "--timeout=1200 --timeout 2700",
        })

        cmd = mock_run_command.call_args[0][0]
        assert cmd.count("--timeout") == 1
        assert cmd[cmd.index("--timeout") + 1] == "2700"

    async def test_create_failure_returns_error(self, mock_run_command_fail):
        result = await blade_create.ainvoke({
            "scope": "pod",
            "target": "network",
            "action": "delay",
            "namespace": "",
            "names": "",
            "labels": "",
            "kubeconfig": "",
            "evict_count": "",
            "evict_percent": "",
            "flags": "",
        })
        assert "Error" in result
        assert "blade create failed" in result

    async def test_create_uses_blade_timeout(self, mock_run_command):
        await blade_create.ainvoke({
            "scope": "pod",
            "target": "network",
            "action": "delay",
            "namespace": "",
            "names": "",
            "labels": "",
            "kubeconfig": "",
            "evict_count": "",
            "evict_percent": "",
            "flags": "",
        })
        call_kwargs = mock_run_command.call_args[1]
        assert call_kwargs.get("timeout") == 30 or "timeout" in call_kwargs

    async def test_create_with_k8s_params(self, mock_run_command):
        await blade_create.ainvoke({
            "scope": "pod",
            "target": "network",
            "action": "delay",
            "namespace": "cms-demo",
            "names": "accounting-7dc7b44956-krtm6",
            "labels": "",
            "kubeconfig": "/Users/test/.kube/config",
            "evict_count": "",
            "evict_percent": "",
            "flags": "--time 3000 --offset 1000",
        })
        cmd = mock_run_command.call_args[0][0]
        assert cmd[2] == "k8s"
        assert cmd[3] == "pod-network"
        assert "--namespace" in cmd
        assert "cms-demo" in cmd
        assert "--names" in cmd
        assert "accounting-7dc7b44956-krtm6" in cmd
        assert "--kubeconfig" in cmd
        assert "/Users/test/.kube/config" in cmd
        assert "--time" in cmd
        assert "3000" in cmd

    async def test_create_with_evict_params(self, mock_run_command):
        await blade_create.ainvoke({
            "scope": "pod",
            "target": "network",
            "action": "delay",
            "namespace": "default",
            "names": "",
            "labels": "app=my-app",
            "kubeconfig": "",
            "evict_count": "2",
            "evict_percent": "",
            "flags": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--namespace" in cmd
        assert "--labels" in cmd
        assert "app=my-app" in cmd
        assert "--evict-count" in cmd
        assert "2" in cmd


class TestBladeCreateNodeScope:
    """Test blade_create tool with node scope — namespace and labels must be omitted."""

    async def test_node_scope_omits_namespace(self, mock_run_command):
        await blade_create.ainvoke({
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
            "names": "cn-hongkong.10.0.2.8",
            "labels": "",
            "kubeconfig": "/Users/test/.kube/config",
            "evict_count": "",
            "evict_percent": "",
            "flags": "--cpu-percent 90",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--namespace" not in cmd
        assert "cms-demo" not in cmd

    async def test_node_scope_omits_labels(self, mock_run_command):
        await blade_create.ainvoke({
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "",
            "names": "cn-hongkong.10.0.2.8",
            "labels": "app=test",
            "kubeconfig": "",
            "evict_count": "",
            "evict_percent": "",
            "flags": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--labels" not in cmd
        assert "app=test" not in cmd

    async def test_node_scope_includes_names(self, mock_run_command):
        await blade_create.ainvoke({
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "",
            "names": "cn-hongkong.10.0.2.8",
            "labels": "",
            "kubeconfig": "",
            "evict_count": "",
            "evict_percent": "",
            "flags": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--names" in cmd
        assert "cn-hongkong.10.0.2.8" in cmd

    async def test_node_scope_full_command(self, mock_run_command):
        await blade_create.ainvoke({
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
            "names": "cn-hongkong.10.0.2.8",
            "labels": "app=test",
            "kubeconfig": "/Users/test/.kube/config",
            "evict_count": "",
            "evict_percent": "",
            "flags": "--cpu-percent 90",
        })
        cmd = mock_run_command.call_args[0][0]
        assert cmd[0] == "blade"
        assert cmd[1] == "create"
        assert cmd[2] == "k8s"
        assert cmd[3] == "node-cpu"
        assert cmd[4] == "fullload"
        assert "--names" in cmd
        assert "cn-hongkong.10.0.2.8" in cmd
        assert "--kubeconfig" in cmd
        assert "--cpu-percent" in cmd
        assert "90" in cmd
        # namespace and labels MUST NOT appear for node scope
        assert "--namespace" not in cmd
        assert "--labels" not in cmd


class TestBladeCreateHostScope:
    """Host scope uses ChaosBlade's OS executor: no k8s domain / scope prefix /
    namespace / labels / kubeconfig. Remote delivery is the host transport."""

    async def test_host_scope_omits_k8s_domain(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        await blade_create.ainvoke({
            "scope": "host",
            "target": "cpu",
            "action": "fullload",
            "namespace": "",
            "names": "10.0.2.8",
            "labels": "",
            "kubeconfig": "",
            "evict_count": "",
            "evict_percent": "",
            "flags": "--cpu-percent 80",
        })
        cmd = mock_run_command.call_args[0][0]
        # `blade create cpu fullload ...` — NO "k8s" domain, NO scope prefix.
        assert cmd[:4] == ["blade", "create", "cpu", "fullload"]
        assert "k8s" not in cmd
        assert "cpu-fullload" not in cmd
        assert "--cpu-percent" in cmd
        assert "80" in cmd
        # --timeout is still auto-injected on the host path.
        assert "--timeout" in cmd

    async def test_host_scope_omits_namespace_labels_kubeconfig(
        self, mock_run_command, monkeypatch,
    ):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "/some/kubeconfig")
        await blade_create.ainvoke({
            "scope": "host",
            "target": "network",
            "action": "drop",
            "namespace": "should-be-ignored",
            "names": "10.0.2.8",
            "labels": "app=ignored",
            "kubeconfig": "/explicit/kubeconfig",
            "evict_count": "1",
            "evict_percent": "50",
            "flags": "--destination-port 3306",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--namespace" not in cmd
        assert "should-be-ignored" not in cmd
        assert "--labels" not in cmd
        assert "app=ignored" not in cmd
        assert "--kubeconfig" not in cmd
        assert "--evict-count" not in cmd
        assert "--evict-percent" not in cmd
        assert "--destination-port" in cmd

    async def test_host_scope_not_bypassed_uses_transport(
        self, mock_run_command, monkeypatch,
    ):
        # Model A: the host transport (SSH) delivers the command, so it must
        # NOT be bypassed the way kubewiz is.
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        await blade_create.ainvoke({
            "scope": "host",
            "target": "cpu",
            "action": "fullload",
            "namespace": "",
            "names": "10.0.2.8",
            "labels": "",
            "kubeconfig": "",
            "evict_count": "",
            "evict_percent": "",
            "flags": "--cpu-percent 80",
        })
        call_kwargs = mock_run_command.call_args[1]
        assert call_kwargs.get("bypass_channel") in (False, None)


class TestBladeDestroy:
    """Test blade_destroy tool function."""

    async def test_successful_destroy(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubeconfig")
        await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": ""})
        mock_run_command.assert_called_once()
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "destroy", "abc123"]

    async def test_destroy_failure_returns_error(self, mock_run_command_fail, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubeconfig")
        result = await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": ""})
        assert "Error" in result
        assert "blade destroy failed" in result

    async def test_destroy_with_kubeconfig(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubeconfig")
        await blade_destroy.ainvoke({
            "uid": "abc123",
            "kubeconfig": "/my/kubeconfig",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_destroy_kubewiz_adds_target_k8s(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "")
        monkeypatch.setattr(_settings, "kubewiz_url", "https://kubewiz.example.com")
        monkeypatch.setattr(_settings, "kubewiz_cluster_uuid", "uuid-123")
        monkeypatch.setattr(_settings, "kubewiz_token", "tok-abc")
        await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        assert "--target" in cmd
        assert "k8s" in cmd
        assert "--kubewiz-url" in cmd

    async def test_destroy_kubewiz_bypasses_channel_wrap(self, mock_run_command, monkeypatch):
        """kubewiz mode: blade reaches KubeWiz Core natively via --kubewiz-url,
        so it must run unwrapped (bypass_channel=True), NOT re-wrapped in
        ``wiz task exec`` — guards against the double-routing regression."""
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "")
        monkeypatch.setattr(_settings, "kubewiz_url", "https://kubewiz.example.com")
        monkeypatch.setattr(_settings, "kubewiz_cluster_uuid", "uuid-123")
        await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": ""})
        assert mock_run_command.call_args.kwargs.get("bypass_channel") is True

    async def test_destroy_kubeconfig_does_not_bypass(self, mock_run_command, monkeypatch):
        """kubeconfig mode: blade runs through the (passthrough) kubeconfig
        channel, so bypass_channel must be False."""
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubeconfig")
        await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": ""})
        assert mock_run_command.call_args.kwargs.get("bypass_channel") is False


class TestBladeStatus:
    """Test blade_status tool function."""

    async def test_status_with_uid(self, mock_run_command):
        await blade_status.ainvoke({"uid": "abc123", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "status", "--uid", "abc123"]

    async def test_status_without_uid(self, mock_run_command):
        await blade_status.ainvoke({"uid": "", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "status"]
        assert "--uid" not in cmd

    async def test_status_with_kubeconfig(self, mock_run_command):
        await blade_status.ainvoke({
            "uid": "abc123",
            "kubeconfig": "/my/kubeconfig",
        })
        cmd = mock_run_command.call_args[0][0]
        # blade status v1.8.0 does NOT support --kubeconfig;
        # kubeconfig is passed via KUBECONFIG env var instead
        assert "--kubeconfig" not in cmd
        kwargs = mock_run_command.call_args[1]
        env_override = kwargs.get("env_override")
        assert env_override == {"KUBECONFIG": "/my/kubeconfig"}


class TestGetHostBladePath:
    """Host scope must use the REMOTE PATH's bare ``blade`` by default and never
    the local ``blade_path`` (which resolves a local absolute path)."""

    def test_defaults_to_bare_blade(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "host_blade_path", "")
        # Even if the local blade_path is set, host must not pick it up.
        monkeypatch.setattr(_settings, "blade_path", "/Users/me/vendor/chaosblade/blade")
        assert _get_host_blade_path() == "blade"

    def test_uses_explicit_host_blade_path(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "host_blade_path", "/opt/chaosblade/blade")
        assert _get_host_blade_path() == "/opt/chaosblade/blade"

    async def test_host_create_uses_host_blade_path(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "host_blade_path", "/opt/chaosblade/blade")
        await blade_create.ainvoke({
            "scope": "host",
            "target": "cpu",
            "action": "fullload",
            "names": "10.0.2.8",
            "flags": "--cpu-percent 80",
        })
        cmd = mock_run_command.call_args[0][0]
        assert cmd[0] == "/opt/chaosblade/blade"
        assert cmd[:4] == ["/opt/chaosblade/blade", "create", "cpu", "fullload"]


class TestBladeDestroyHostScope:
    """Host scope: the experiment lives in the remote host's local DB, so
    destroy must run there (wiz-wrapped) with the bare remote blade — NO
    --target k8s / kubewiz / kubeconfig args, and bypass_channel MUST be False."""

    async def test_host_destroy_bare_command(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubewiz_host")
        monkeypatch.setattr(_settings, "host_name", "10.0.2.8")
        await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": "/ignored"})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "destroy", "abc123"]
        assert "--target" not in cmd
        assert "--kubeconfig" not in cmd
        assert "--kubewiz-url" not in cmd

    async def test_host_destroy_not_bypassed(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubewiz_host")
        monkeypatch.setattr(_settings, "host_name", "10.0.2.8")
        await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": ""})
        assert mock_run_command.call_args.kwargs.get("bypass_channel") is False


class TestBladeStatusHostScope:
    """Host scope: query the remote host's local DB directly (no `blade query
    k8s`, no KUBECONFIG env), wiz-wrapped (bypass_channel=False)."""

    async def test_host_status_with_uid(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubewiz_host")
        monkeypatch.setattr(_settings, "host_name", "10.0.2.8")
        await blade_status.ainvoke({"uid": "abc123", "kubeconfig": "/ignored"})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "status", "--uid", "abc123"]
        assert "query" not in cmd
        kwargs = mock_run_command.call_args[1]
        assert kwargs.get("bypass_channel") is False
        assert kwargs.get("env_override") in (None, {})

    async def test_host_status_without_uid(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubewiz_host")
        monkeypatch.setattr(_settings, "host_name", "10.0.2.8")
        await blade_status.ainvoke({"uid": "", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "status"]
        assert "--uid" not in cmd


class TestBladeHelp:
    """Test blade_help tool function."""

    async def test_help_toplevel(self, mock_run_command):
        await blade_help.ainvoke({"subcommand": ""})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "-h"]

    async def test_help_create(self, mock_run_command):
        await blade_help.ainvoke({"subcommand": "create"})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "create", "-h"]

    async def test_help_deep_subcommand(self, mock_run_command):
        await blade_help.ainvoke({"subcommand": "create k8s pod-network drop"})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "create", "k8s", "pod-network", "drop", "-h"]

    async def test_help_filters_flags(self, mock_run_command):
        await blade_help.ainvoke({"subcommand": "create k8s --names foo"})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "create", "k8s", "foo", "-h"]
        assert "--names" not in cmd

    async def test_help_deduplicates_h(self, mock_run_command):
        await blade_help.ainvoke({"subcommand": "create -h"})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "create", "-h"]
        assert cmd.count("-h") == 1

    async def test_help_short_timeout(self, mock_run_command):
        await blade_help.ainvoke({"subcommand": "create"})
        call_kwargs = mock_run_command.call_args[1]
        assert call_kwargs.get("timeout") == 10

    async def test_help_exception(self, mocker):
        import chaos_agent.tools.blade as blade_mod
        mocker.patch.object(blade_mod, "_get_blade_path", return_value="blade")
        mocker.patch.object(blade_mod, "execute_via_transport", side_effect=RuntimeError("no blade"))
        result = await blade_help.ainvoke({"subcommand": "create"})
        assert "Error" in result


class TestBladeQueryK8s:
    """Test blade_query_k8s tool function."""

    async def test_query_with_uid(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        await blade_query_k8s.ainvoke({"uid": "abc123", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        # blade query k8s create <uid> — ChaosBlade K8s query format
        assert cmd == ["blade", "query", "k8s", "create", "abc123"]

    async def test_query_without_uid(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        await blade_query_k8s.ainvoke({"uid": "", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "query", "k8s"]

    async def test_query_with_kubeconfig(self, mock_run_command):
        await blade_query_k8s.ainvoke({
            "uid": "abc123",
            "kubeconfig": "/my/kubeconfig",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_host_scope_returns_guidance_without_running(self, mock_run_command, monkeypatch):
        # Host experiments have no cluster CRD/selector to query. The tool must
        # NOT misroute a `blade query k8s` command onto the host channel; it
        # returns guidance pointing to blade_status and never touches transport.
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubewiz_host")
        monkeypatch.setattr(_settings, "host_name", "10.0.2.8")
        out = await blade_query_k8s.ainvoke({"uid": "abc123", "kubeconfig": "/ignored"})
        assert "blade_status" in out
        assert "host-scope" in out
        mock_run_command.assert_not_called()
