"""Tests for kubectl CLI tool wrapper."""

import json
import sys

import pytest

from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.kubectl import (
    READONLY_SUBCOMMANDS,
    _build_kubectl_global_args,
    _is_json_output,
    _split_args,
    kubectl,
    kubectl_read,
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "nodes -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "nodes" in cmd

    async def test_get_with_label_selector(self, mock_run_command):
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default --field-selector=status.phase=Pending -o json",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--field-selector=status.phase=Pending" in cmd

    async def test_kubeconfig_injected(self, mock_run_command):
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        # exec subcommand should use timeout_kubectl_exec (180s by default)
        assert call_kwargs.get("timeout") == 180

    async def test_exec_with_kubeconfig(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "/my/kubeconfig",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_exec_timeout_surfaces_raw_signal_no_failed_verdict(self, monkeypatch):
        # P-A/1a: a self-severing injection's exec times out ON SUCCESS. The
        # tool must surface the raw timeout text without an editorial "failed"
        # verdict, while keeping the "Error:" failure-marker contract.
        async def fake_run(cmd, *args, **kwargs):
            raise Exception("Command timed out after 10s")

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- iptables -A INPUT -j DROP",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert result.startswith("Error:")
        assert "Command timed out after 10s" in result
        assert "kubectl exec failed:" not in result

    async def test_exec_nonzero_exit_reports_code_and_raw_output(self, mock_run_command_fail):
        # Non-zero exit surfaces the exit code + raw stderr verbatim, without a
        # "failed" verdict word (the raw output speaks; the LLM judges).
        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        assert result.startswith("Error:")
        assert "(exit 1)" in result
        assert "command failed" in result  # raw stderr preserved
        assert "kubectl exec failed:" not in result


class TestKubectlDebugLifecycle:
    """Debug pods use their real namespace and must be Ready before return."""

    @pytest.mark.asyncio
    async def test_resolves_namespace_waits_and_returns_identity(self, monkeypatch):
        calls = []

        async def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            command = " ".join(cmd)
            if " config " in f" {command} ":
                stdout = "kubewiz"
            elif " debug " in f" {command} ":
                stdout = (
                    "Creating debugging pod node-debugger-node-a-abc12 "
                    "with container debugger on node node-a."
                )
            elif " wait " in f" {command} ":
                stdout = "pod/node-debugger-node-a-abc12 condition met"
            else:
                stdout = json.dumps({
                    "metadata": {
                        "name": "node-debugger-node-a-abc12",
                        "namespace": "kubewiz",
                        "uid": "uid-debug-1",
                    },
                    "spec": {
                        "nodeName": "node-a",
                        "containers": [{
                            "securityContext": {"privileged": True},
                        }],
                    },
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [{"ready": True, "state": {}}],
                    },
                })
            return CommandResult(0, stdout, "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a --image=local/debug -- sleep 900",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })

        debug_cmd = next(cmd for cmd in calls if "debug" in cmd)
        assert debug_cmd[debug_cmd.index("-n") + 1] == "kubewiz"
        assert debug_cmd.index("-n") < debug_cmd.index("--")
        assert '"namespace":"kubewiz"' in result
        assert '"uid":"uid-debug-1"' in result
        assert '"node":"node-a"' in result
        assert '"privileged":true' in result
        assert '"ready":true' in result

    @pytest.mark.asyncio
    async def test_unready_debug_pod_returns_structured_error(self, monkeypatch):
        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod node-debugger-node-a-bad12 "
                    "with container debugger on node node-a.",
                    "",
                    1.0,
                )
            if " wait " in f" {command} ":
                return CommandResult(1, "", "timed out", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            return CommandResult(
                0,
                json.dumps({
                    "metadata": {
                        "name": "node-debugger-node-a-bad12",
                        "namespace": "test-ns",
                        "uid": "uid-debug-bad",
                    },
                    "spec": {"nodeName": "node-a"},
                    "status": {
                        "phase": "Pending",
                        "containerStatuses": [{
                            "state": {"waiting": {"reason": "ImagePullBackOff"}}
                        }],
                    },
                }),
                "",
                1.0,
            )

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns --image=bad/image -- sleep 900",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })

        assert result.startswith("Error:")
        assert "ImagePullBackOff" in result
        assert '"namespace":"test-ns"' in result
        assert '"ready":false' in result
        assert '"cleaned":true' in result
        assert "Do NOT call kubectl exec" in result
        assert "cleaned up automatically" in result

    @pytest.mark.asyncio
    async def test_pod_scoped_debug_resolves_ephemeral_container(self, monkeypatch):
        # Pod-scoped ``kubectl debug <pod> --target=`` attaches an ephemeral
        # container; kubectl prints no name (only "Targeting container ...").
        # The tool must resolve the name from ephemeralContainerStatuses, report
        # the exec handle, and NEVER delete the target pod. Regression for
        # task-3a360709 [139] where this looped as "created no identifiable pod".
        calls = []

        async def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            command = " ".join(cmd)
            if " config " in f" {command} ":
                return CommandResult(0, "arms-prom", "", 1.0)
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    'Targeting container "app". If you don\'t see processes '
                    "from this container it may be because the container "
                    "runtime doesn't support this feature.\n",
                    "", 1.0,
                )
            # get pod -o json → target pod with a running ephemeral container
            return CommandResult(0, json.dumps({
                "metadata": {"name": "p0", "namespace": "arms-prom", "uid": "u-9"},
                "spec": {
                    "nodeName": "node-a",
                    "ephemeralContainers": [{
                        "name": "debugger-xy12",
                        "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
                    }],
                },
                "status": {
                    "phase": "Running",
                    "ephemeralContainerStatuses": [{
                        "name": "debugger-xy12",
                        "state": {"running": {"startedAt": "now"}},
                    }],
                },
            }), "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": ("p0 -n arms-prom --image=img --target=app "
                       "--profile=netadmin --quiet -- sleep 1800"),
            "kubeconfig": "", "context": "", "cluster": "",
        })

        # Resolved the ephemeral container name and gave the exec handle.
        assert '"ephemeral_container":"debugger-xy12"' in result
        assert "-c debugger-xy12" in result
        # It is NOT the misleading "created no identifiable pod".
        assert "no identifiable pod" not in result
        # The target pod (user workload) must NEVER be deleted.
        assert not any("delete" in " ".join(c) for c in calls), \
            "target pod must not be deleted"
        # Cleanup guidance must say do-not-delete.
        assert "do NOT delete the pod" in result

    @pytest.mark.asyncio
    async def test_pod_scoped_debug_reports_name_location_when_not_running(
        self, monkeypatch,
    ):
        # Ephemeral container created but not yet running: the error must point
        # at the target pod's status (not claim the pod was not created) and
        # must forbid deleting the target pod.
        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " config " in f" {command} ":
                return CommandResult(0, "arms-prom", "", 1.0)
            if " debug " in f" {command} ":
                return CommandResult(0, 'Targeting container "app".\n', "", 1.0)
            # ephemeral container present but still pulling its image
            return CommandResult(0, json.dumps({
                "metadata": {"name": "p0", "namespace": "arms-prom", "uid": "u-9"},
                "spec": {"nodeName": "node-a", "ephemeralContainers": [
                    {"name": "debugger-xy12"}]},
                "status": {"phase": "Running", "ephemeralContainerStatuses": [{
                    "name": "debugger-xy12",
                    "state": {"waiting": {"reason": "ImagePullBackOff"}},
                }]},
            }), "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        # Keep the not-running poll bounded to one pass: a 1s deadline plus a
        # no-op sleep so the loop exits immediately instead of busy-waiting 60s.
        monkeypatch.setattr(kubectl_mod.settings, "timeout_kubectl_exec", 1)

        async def _no_sleep(_):
            return None
        monkeypatch.setattr(kubectl_mod.asyncio, "sleep", _no_sleep)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "p0 -n arms-prom --image=img --target=app -- sleep 1800",
            "kubeconfig": "", "context": "", "cluster": "",
        })
        assert result.startswith("Error:")
        assert "debugger-xy12" in result
        assert "ImagePullBackOff" in result
        assert "Do NOT delete the target pod" in result


class TestKubectlPatch:
    """Test kubectl tool with subcommand='patch'."""

    async def test_json_patch(self, mock_run_command):
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment my-deploy -n default --replicas=0",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--replicas=0" in cmd

    async def test_scale_by_label_selector(self, mock_run_command):
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        from chaos_agent.config.settings import settings as _settings
        assert call_kwargs.get("timeout") == _settings.timeout_kubectl

    async def test_exec_uses_longer_timeout(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "",
            "context": "",
            "cluster": "",
        })
        call_kwargs = mock_run_command.call_args[1]
        assert call_kwargs.get("timeout") == 180


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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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
        await kubectl.ainvoke({
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


# ============================================================================
# kubectl_read — the single read-only kubectl (planning / intent / verify)
#
# Tests focus on what distinguishes kubectl_read from the full kubectl:
#   - Literal subcommand constraint matches READONLY_SUBCOMMANDS (read verbs
#     + exec + debug)
#   - Runtime defensive check rejects mutating subcommands outside the Literal
#   - exec/debug inner commands are gated to read-only probes (specific reason)
#   - Legitimate read-only calls delegate to the full kubectl correctly
# ============================================================================


class TestKubectlReadSubcommandTable:
    """Literal type annotation and the runtime allowlist must stay in sync."""

    def test_literal_matches_runtime_allowlist(self):
        schema = kubectl_read.args_schema.model_json_schema()
        enum_values = schema["properties"]["subcommand"].get("enum", [])
        assert set(enum_values) == set(READONLY_SUBCOMMANDS), (
            f"kubectl_read Literal/enum {enum_values} drifted from "
            f"READONLY_SUBCOMMANDS {READONLY_SUBCOMMANDS}"
        )

    def test_includes_exec_and_debug(self):
        assert "exec" in READONLY_SUBCOMMANDS
        assert "debug" in READONLY_SUBCOMMANDS

    def test_excludes_mutating_subcommands(self):
        mutating = {
            "delete", "patch", "apply", "scale", "taint",
            "cordon", "drain", "rollout", "edit", "replace",
            "run", "create", "label", "annotate", "expose",
        }
        assert mutating.isdisjoint(set(READONLY_SUBCOMMANDS)), (
            f"READONLY_SUBCOMMANDS leaked a mutating subcommand: "
            f"{mutating & set(READONLY_SUBCOMMANDS)}"
        )


class TestKubectlReadRuntimeDefence:
    """Runtime checks reject mutating subcommands and mutating exec inner
    commands even if the Literal validation is bypassed."""

    @pytest.mark.asyncio
    async def test_runtime_rejects_mutating_subcommand(self):
        result = await kubectl_read.coroutine(subcommand="delete", v_args="pod x")
        assert "Error" in result
        assert "read-only" in result.lower()
        for sub in READONLY_SUBCOMMANDS:
            assert sub in result  # the allowlist is shown to the LLM

    @pytest.mark.asyncio
    async def test_exec_mutating_inner_rejected_with_reason(self):
        result = await kubectl_read.coroutine(
            subcommand="exec",
            v_args="my-pod -n ns -- iptables -A INPUT -j DROP",
        )
        assert "Error" in result
        assert "not read-only" in result.lower()
        assert "iptables" in result  # names the specific offending binary

    @pytest.mark.asyncio
    async def test_debug_mutating_inner_rejected(self):
        result = await kubectl_read.coroutine(
            subcommand="debug",
            v_args="node/n1 --image=busybox -- dd if=/dev/zero of=/x",
        )
        assert "Error" in result
        assert "not read-only" in result.lower()


class TestKubectlReadDelegation:
    """Legitimate read-only calls produce the same command line as the full
    kubectl tool — we delegate to it internally."""

    @pytest.mark.asyncio
    async def test_get_delegates_to_kubectl(self, mock_run_command):
        await kubectl_read.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n cms-demo",
            "kubeconfig": "/kc",
            "context": "",
            "cluster": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert cmd[0].endswith("kubectl")
        assert "--kubeconfig" in cmd and "/kc" in cmd
        assert "get" in cmd
        assert "pods" in cmd and "-n" in cmd and "cms-demo" in cmd

    @pytest.mark.asyncio
    async def test_describe_delegates(self, mock_run_command):
        await kubectl_read.ainvoke({
            "subcommand": "describe",
            "v_args": "pod my-pod -n ns",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "describe" in cmd
        assert "pod" in cmd and "my-pod" in cmd

    @pytest.mark.asyncio
    async def test_top_delegates(self, mock_run_command):
        await kubectl_read.ainvoke({
            "subcommand": "top",
            "v_args": "pod accounting-x -n cms-demo",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "top" in cmd

    @pytest.mark.asyncio
    async def test_exec_readonly_inner_delegates(self, mock_run_command):
        await kubectl_read.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n ns -- cat /proc/diskstats",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "exec" in cmd and "cat" in cmd

    @pytest.mark.asyncio
    async def test_exec_iptables_list_delegates(self, mock_run_command):
        await kubectl_read.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n ns -- iptables -L",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "exec" in cmd and "iptables" in cmd


class TestGuardRejectionReachesTheModelIntact:
    """The guard's feedback must survive the tool layer that wraps it.

    ``ToolGuard`` builds a ``GuardFeedback`` whose ``compliant_form`` carries the
    way forward, but the model never sees that object: it sees a string produced
    by ``render_for_llm()`` → ``ToolGuardError`` → this tool's ``except`` →
    ``f"Error: kubectl {subcommand}: {e}"``. Three layers, any of which could
    truncate or replace it — and the whole point of stating the allow-list is
    lost if it is dropped in transit. task-c758cdbd's message
    (``Error: kubectl label: kubectl subcommand not allowed: label``) came
    through this exact path.

    No transport mock is needed: the guard rejects BEFORE dispatch, so nothing
    is executed and no cluster is contacted.
    """

    @pytest.mark.asyncio
    async def test_subcommand_rejection_carries_the_allow_list(self):
        out = await kubectl.ainvoke({"subcommand": "edit", "v_args": "deployment x"})
        assert out.startswith("Error: kubectl edit:")
        assert "subcommand not allowed: edit" in out
        # The allow-list (compliant_form) must not be lost in the wrapping.
        assert "Allowed subcommands:" in out
        for sub in ("label", "patch", "drain"):
            assert sub in out

    @pytest.mark.asyncio
    async def test_drain_flag_rejection_carries_cause_and_way_forward(self):
        out = await kubectl.ainvoke({"subcommand": "drain", "v_args": "n1 --force"})
        assert "--force not allowed" in out          # reason
        assert "NO owning controller" in out         # the specific cause
        assert "Drop the flag" in out                # compliant_form

    @pytest.mark.asyncio
    async def test_config_write_rejection_carries_the_alternative(self):
        out = await kubectl.ainvoke({
            "subcommand": "config", "v_args": "use-context other",
        })
        assert "only allows read-only 'view'" in out
        assert "--context/--kubeconfig" in out
