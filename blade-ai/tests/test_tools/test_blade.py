"""Tests for ChaosBlade CLI tool wrappers."""

from chaos_agent.tools.blade import _build_kubeconfig_arg, blade_create, blade_destroy, blade_help, blade_query_k8s, blade_status


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
        result = await blade_create.ainvoke({
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
        result = await blade_create.ainvoke({
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
        result = await blade_create.ainvoke({
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
        result = await blade_create.ainvoke({
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
        result = await blade_create.ainvoke({
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
        result = await blade_create.ainvoke({
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
        result = await blade_create.ainvoke({
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


class TestBladeDestroy:
    """Test blade_destroy tool function."""

    async def test_successful_destroy(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        result = await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": ""})
        mock_run_command.assert_called_once()
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "destroy", "abc123"]

    async def test_destroy_failure_returns_error(self, mock_run_command_fail):
        result = await blade_destroy.ainvoke({"uid": "abc123", "kubeconfig": ""})
        assert "Error" in result
        assert "blade destroy failed" in result

    async def test_destroy_with_kubeconfig(self, mock_run_command):
        result = await blade_destroy.ainvoke({
            "uid": "abc123",
            "kubeconfig": "/my/kubeconfig",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd


class TestBladeStatus:
    """Test blade_status tool function."""

    async def test_status_with_uid(self, mock_run_command):
        result = await blade_status.ainvoke({"uid": "abc123", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "status", "--uid", "abc123"]

    async def test_status_without_uid(self, mock_run_command):
        result = await blade_status.ainvoke({"uid": "", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "status"]
        assert "--uid" not in cmd

    async def test_status_with_kubeconfig(self, mock_run_command):
        result = await blade_status.ainvoke({
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
        mocker.patch.object(blade_mod, "run_command", side_effect=RuntimeError("no blade"))
        result = await blade_help.ainvoke({"subcommand": "create"})
        assert "Error" in result


class TestBladeQueryK8s:
    """Test blade_query_k8s tool function."""

    async def test_query_with_uid(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        result = await blade_query_k8s.ainvoke({"uid": "abc123", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        # blade query k8s create <uid> — ChaosBlade K8s query format
        assert cmd == ["blade", "query", "k8s", "create", "abc123"]

    async def test_query_without_uid(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        result = await blade_query_k8s.ainvoke({"uid": "", "kubeconfig": ""})
        cmd = mock_run_command.call_args[0][0]
        assert cmd == ["blade", "query", "k8s"]

    async def test_query_with_kubeconfig(self, mock_run_command):
        result = await blade_query_k8s.ainvoke({
            "uid": "abc123",
            "kubeconfig": "/my/kubeconfig",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd
