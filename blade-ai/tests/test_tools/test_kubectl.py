"""Tests for kubectl CLI tool wrapper."""

from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.kubectl import (
    _build_kubectl_global_args,
    _is_json_output,
    _split_args,
    kubectl,
)


class TestBuildKubectlGlobalArgs:
    """Test _build_kubectl_global_args helper."""

    def test_all_empty(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        assert _build_kubectl_global_args() == []

    def test_kubeconfig_explicit(self):
        result = _build_kubectl_global_args(kubeconfig="/path/to/kubeconfig")
        assert result == ["--kubeconfig", "/path/to/kubeconfig"]

    def test_context_explicit(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        result = _build_kubectl_global_args(context="my-context")
        assert result == ["--context", "my-context"]

    def test_cluster_explicit(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        result = _build_kubectl_global_args(cluster="my-cluster")
        assert result == ["--cluster", "my-cluster"]

    def test_all_global_flags(self):
        result = _build_kubectl_global_args(
            kubeconfig="/path/kc", context="ctx", cluster="cl"
        )
        assert result == [
            "--kubeconfig", "/path/kc",
            "--context", "ctx",
            "--cluster", "cl",
        ]

    def test_kubeconfig_settings_fallback(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "/from/settings")
        result = _build_kubectl_global_args()
        assert result == ["--kubeconfig", "/from/settings"]

    def test_kubeconfig_env_fallback(self, monkeypatch):
        from chaos_agent.config.settings import Settings, settings as _settings
        monkeypatch.delenv("BLADE_AI_KUBECONFIG_PATH", raising=False)
        monkeypatch.setenv("KUBECONFIG", "/from/env")
        s = Settings()
        assert s.kubeconfig_path == "/from/env"
        monkeypatch.setattr(_settings, "kubeconfig_path", "/from/env")
        result = _build_kubectl_global_args()
        assert result == ["--kubeconfig", "/from/env"]

    def test_explicit_overrides_settings(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "/from/settings")
        result = _build_kubectl_global_args(kubeconfig="/explicit")
        assert result == ["--kubeconfig", "/explicit"]


class TestIsJsonOutput:
    """Test _is_json_output helper."""

    def test_dash_o_json(self):
        assert _is_json_output("pods -n default -o json") is True

    def test_dash_o_yaml(self):
        assert _is_json_output("pods -n default -o yaml") is False

    def test_no_output_flag(self):
        assert _is_json_output("pods -n default") is False

    def test_jsonpath(self):
        assert _is_json_output("pods -n default -o jsonpath='{.items[*].metadata.name}'") is False

    def test_dash_o_equals_json(self):
        assert _is_json_output("pods -n default -o=json") is True

    def test_wide(self):
        assert _is_json_output("pods -n default -o wide") is False


class TestKubectlGet:
    """Test kubectl tool with subcommand='get'."""

    async def test_get_pods_with_namespace(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert cmd[0] == "kubectl"
        assert "get" in cmd
        assert "pods" in cmd
        assert "-n" in cmd
        assert "default" in cmd
        assert "-o" in cmd
        assert "json" in cmd

    async def test_get_nodes(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "nodes -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "nodes" in cmd

    async def test_get_with_label_selector(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -l app=my-app -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "-l" in cmd
        assert "app=my-app" in cmd

    async def test_get_with_field_selector(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default --field-selector=status.phase=Pending -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--field-selector=status.phase=Pending" in cmd

    async def test_kubeconfig_injected(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "/my/kubeconfig",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_context_injected(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
            "context": "prod-ctx",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--context" in cmd
        assert "prod-ctx" in cmd

    async def test_failure_returns_error(self, mock_run_command_fail):
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert "Error" in result


class TestKubectlDescribe:
    """Test kubectl tool with subcommand='describe'."""

    async def test_describe_with_namespace(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "describe",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert cmd[0] == "kubectl"
        assert "describe" in cmd
        assert "pod" in cmd
        assert "my-pod" in cmd
        assert "-n" in cmd
        assert "default" in cmd

    async def test_describe_without_namespace(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "describe",
            "v_args": "node worker-1",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "describe" in cmd
        assert "node" in cmd
        assert "worker-1" in cmd
        assert "-n" not in cmd

    async def test_describe_with_kubeconfig(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "describe",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "/my/kubeconfig",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd


class TestKubectlExec:
    """Test kubectl tool with subcommand='exec'."""

    async def test_exec_command(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ping -c 3 google.com",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert cmd[0] == "kubectl"
        assert "exec" in cmd
        assert "my-pod" in cmd
        assert "-n" in cmd
        assert "default" in cmd
        assert "--" in cmd
        assert "ping" in cmd

    async def test_exec_uses_longer_timeout(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        call_kwargs = mock_run_command.call_args[1]
        # exec subcommand should use timeout_kubectl_exec (60s by default)
        assert call_kwargs.get("timeout") == 60

    async def test_exec_with_kubeconfig(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "/my/kubeconfig",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd


class TestKubectlPatch:
    """Test kubectl tool with subcommand='patch'."""

    async def test_json_patch(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "patch",
            "v_args": 'pod my-pod -n default --type=json -p \'[{"op":"add","path":"/metadata/finalizers","value":["chaos-test/block"]}]\'',
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "patch" in cmd
        assert "pod" in cmd
        assert "my-pod" in cmd
        assert "--type=json" in cmd

    async def test_strategic_merge_patch(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "patch",
            "v_args": 'pod my-pod -n default -p \'{"metadata":{"labels":{"chaos":"true"}}}\'',
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "patch" in cmd
        assert "--type" not in cmd

    async def test_patch_with_kubeconfig(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "patch",
            "v_args": 'pod my-pod -n default --type=json -p \'[{"op":"remove","path":"/metadata/finalizers"}]\'',
            "kubeconfig": "/my/kubeconfig",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_failure_returns_error(self, mock_run_command_fail):
        result = await kubectl.ainvoke({
            "subcommand": "patch",
            "v_args": 'pod my-pod -n default -p \'{"metadata":{}}\'',
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert "Error" in result


class TestKubectlDelete:
    """Test kubectl tool with subcommand='delete'."""

    async def test_delete_pod_by_name(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "delete" in cmd
        assert "pod" in cmd
        assert "my-pod" in cmd
        assert "-n" in cmd
        assert "default" in cmd

    async def test_delete_by_label_selector(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod -n default -l app=my-app",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "-l" in cmd
        assert "app=my-app" in cmd

    async def test_force_delete(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod my-pod -n default --force --grace-period=0",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--force" in cmd
        assert "--grace-period=0" in cmd

    async def test_delete_with_kubeconfig(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "/my/kubeconfig",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_failure_returns_error(self, mock_run_command_fail):
        result = await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert "Error" in result


class TestKubectlScale:
    """Test kubectl tool with subcommand='scale'."""

    async def test_scale_deployment_by_name(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment my-deploy -n default --replicas=3",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert cmd[0] == "kubectl"
        assert "scale" in cmd
        assert "deployment" in cmd
        assert "my-deploy" in cmd
        assert "--replicas=3" in cmd
        assert "-n" in cmd
        assert "default" in cmd

    async def test_scale_to_zero(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment my-deploy -n default --replicas=0",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--replicas=0" in cmd

    async def test_scale_by_label_selector(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment -n default -l app=my-app --replicas=1",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "scale" in cmd
        assert "-l" in cmd
        assert "app=my-app" in cmd

    async def test_scale_with_kubeconfig(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment my-deploy -n default --replicas=3",
            "kubeconfig": "/my/kubeconfig",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_failure_returns_error(self, mock_run_command_fail):
        result = await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment my-deploy -n default --replicas=3",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert "Error" in result


class TestKubectlCordonUncordon:
    """Test kubectl tool with subcommand='cordon'/'uncordon'."""

    async def test_cordon_node(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "cordon",
            "v_args": "my-node",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "cordon" in cmd
        assert "my-node" in cmd

    async def test_uncordon_node(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "uncordon",
            "v_args": "my-node",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "uncordon" in cmd
        assert "my-node" in cmd


class TestKubectlTaint:
    """Test kubectl tool with subcommand='taint'."""

    async def test_taint_add(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "taint",
            "v_args": "nodes my-node key=value:NoSchedule",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "taint" in cmd
        assert "nodes" in cmd
        assert "my-node" in cmd
        assert "key=value:NoSchedule" in cmd

    async def test_taint_remove(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "taint",
            "v_args": "nodes my-node key-",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "taint" in cmd
        assert "key-" in cmd


class TestKubectlLargeOutput:
    """Test large output optimization for get subcommand with -o json."""

    async def test_large_json_output_appends_hint(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubectl_max_output_bytes", 100)
        large_json = '{"items": [' + ",".join(['{"kind": "Pod"}'] * 50) + "]}"
        mock_run_command.side_effect = None
        mock_run_command.return_value = CommandResult(
            exit_code=0, stdout=large_json, stderr="", duration_ms=100.0,
        )
        import sys
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
        mocker_patch = monkeypatch.setattr(kubectl_mod, "run_command", mock_run_command)

        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert "LARGE_OUTPUT" in result

    async def test_small_json_output_no_hint(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubectl_max_output_bytes", 32768)
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert "LARGE_OUTPUT" not in result

    async def test_non_json_output_no_hint(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubectl_max_output_bytes", 1)
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o wide",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert "LARGE_OUTPUT" not in result


class TestKubectlTimeouts:
    """Test that exec subcommand uses longer timeout."""

    async def test_get_uses_default_timeout(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        call_kwargs = mock_run_command.call_args[1]
        assert call_kwargs.get("timeout") == 30

    async def test_exec_uses_longer_timeout(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        call_kwargs = mock_run_command.call_args[1]
        assert call_kwargs.get("timeout") == 60


class TestSplitArgs:
    """Test _split_args helper for shell-aware argument splitting."""

    def test_simple_args(self):
        assert _split_args("pods -n default -o json") == [
            "pods", "-n", "default", "-o", "json",
        ]

    def test_empty_string(self):
        assert _split_args("") == []

    def test_jsonpath_single_quoted(self):
        """Single quotes around jsonpath should be stripped (shell quoting)."""
        result = _split_args("pods -o jsonpath='{.spec.replicas}'")
        assert result == ["pods", "-o", "jsonpath={.spec.replicas}"]

    def test_jsonpath_double_quoted(self):
        """Double quotes around jsonpath should be stripped."""
        result = _split_args('pods -o jsonpath="{.spec.replicas}"')
        assert result == ["pods", "-o", "jsonpath={.spec.replicas}"]

    def test_jsonpath_unquoted(self):
        """Unquoted jsonpath should pass through unchanged."""
        result = _split_args("pods -o jsonpath={.spec.replicas}")
        assert result == ["pods", "-o", "jsonpath={.spec.replicas}"]

    def test_patch_json_single_quoted(self):
        """Single-quoted JSON patch payload should have quotes stripped."""
        result = _split_args("""pod my-pod -n ns -p '{"metadata":{"labels":{"chaos":"true"}}}'""")
        assert result == [
            "pod", "my-pod", "-n", "ns", "-p",
            '{"metadata":{"labels":{"chaos":"true"}}}',
        ]

    def test_unmatched_quote_fallback(self):
        """Unmatched quotes should fallback to str.split() instead of raising."""
        result = _split_args("pods -o jsonpath='{.spec.replicas")
        # shlex.split would raise ValueError; fallback to str.split()
        assert "pods" in result
        assert "-o" in result

    def test_no_quotes_same_as_str_split(self):
        """For unquoted args, _split_args should match str.split()."""
        args = "pods -n default -l app=nginx -o wide"
        assert _split_args(args) == args.split()


class TestKubectlJsonpathQuoting:
    """Test that kubectl tool correctly passes jsonpath args with shell quoting."""

    async def test_jsonpath_quoted_arg_stripped(self, mock_run_command):
        """jsonpath='{.spec.replicas}' should be passed as jsonpath={.spec.replicas}."""
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "deployments -n ns my-deploy -o jsonpath='{.spec.replicas}'",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        # The argument after -o should NOT contain literal single quotes
        o_index = cmd.index("-o")
        jsonpath_arg = cmd[o_index + 1]
        assert jsonpath_arg == "jsonpath={.spec.replicas}"
        assert "'" not in jsonpath_arg

    async def test_jsonpath_wildcard_quoted(self, mock_run_command):
        """jsonpath='{.items[*].metadata.name}' should strip quotes."""
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n ns -o jsonpath='{.items[*].metadata.name}'",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        o_index = cmd.index("-o")
        jsonpath_arg = cmd[o_index + 1]
        assert jsonpath_arg == "jsonpath={.items[*].metadata.name}"

    async def test_patch_json_payload_quoted(self, mock_run_command):
        """Patch with quoted JSON payload should strip outer quotes."""
        result = await kubectl.ainvoke({
            "subcommand": "patch",
            "v_args": """pod my-pod -n ns -p '{"metadata":{"labels":{"chaos":"true"}}}'""",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        p_index = cmd.index("-p")
        patch_arg = cmd[p_index + 1]
        # Outer quotes stripped, inner JSON structure preserved
        assert patch_arg == '{"metadata":{"labels":{"chaos":"true"}}}'
        assert not patch_arg.startswith("'")

    async def test_jsonpath_multi_field_with_space_literal(self, mock_run_command):
        """jsonpath with space literal in curly braces should be a single token.

        This was the root cause of the session ses-ad1c95c2 JSONPath errors:
        LLM generated expressions like {"spec.replicas: "} where the space
        after the colon caused simple split() to break the token.
        With shlex.split(), single quotes protect the entire expression.
        """
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": """deployment my-deploy -n ns -o jsonpath='{"spec.replicas: "}{.spec.replicas}{"\\nstatus.replicas: "}{.status.replicas}'""",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        o_index = cmd.index("-o")
        jsonpath_arg = cmd[o_index + 1]
        # The entire jsonpath expression should be a single token
        assert jsonpath_arg.startswith("jsonpath=")
        # Should contain the space literal from {"spec.replicas: "}
        assert '{"spec.replicas: "}' in jsonpath_arg
        # Should NOT be split across multiple tokens
        assert ".spec.replicas" in jsonpath_arg

    async def test_jsonpath_newline_separator(self, mock_run_command):
        """jsonpath with newline separator {"\\n"} should be a single token."""
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": """deployment my-deploy -n ns -o jsonpath='{.spec.replicas}{"\\n"}{.status.readyReplicas}'""",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        o_index = cmd.index("-o")
        jsonpath_arg = cmd[o_index + 1]
        assert jsonpath_arg.startswith("jsonpath=")
        assert ".spec.replicas" in jsonpath_arg
        assert ".status.readyReplicas" in jsonpath_arg

    async def test_kubeconfig_in_v_args_stripped(self, mock_run_command):
        """If LLM embeds --kubeconfig in v_args, it should be stripped with a warning."""
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default --kubeconfig /should/be/stripped",
            "kubeconfig": "/explicit/kubeconfig",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        # The v_args kubeconfig should be removed; only the parameter one should remain
        kubeconfig_indices = [i for i, x in enumerate(cmd) if x == "--kubeconfig"]
        # Should have exactly one --kubeconfig (from the parameter)
        assert len(kubeconfig_indices) == 1
        assert cmd[kubeconfig_indices[0] + 1] == "/explicit/kubeconfig"
