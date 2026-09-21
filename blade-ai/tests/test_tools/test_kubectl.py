"""Tests for kubectl CLI tool wrapper."""

import json
import sys
import time
from datetime import datetime, timezone

import pytest

from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.kubectl_cli import (
    EMPTY_SELECTOR_HINT,
    READONLY_SUBCOMMANDS,
    _build_kubectl_global_args,
    _is_json_output,
    _split_args,
    kubectl,
    kubectl_read,
)


class TestBuildKubectlGlobalArgs:
    """Test _build_kubectl_global_args helper."""

    def test_kubectl_docstring_claims_no_timeout_boost(self):
        # inject-aac02265 (#35 round audit): the kubectl docstring taught
        # "`exec ... blade create` auto-injects/boosts `--timeout` (may
        # lengthen, not shorten)" — the boost policy no longer exists:
        # ensure_min_duration honours explicit values verbatim. The purge
        # directive: no LLM-facing surface may teach raise-the-floor
        # semantics. (The absent-timeout auto-inject behaviour itself is
        # unchanged and covered by behaviour tests below.)
        assert "boosts" not in kubectl.__doc__
        assert "may lengthen" not in kubectl.__doc__

    def test_all_empty(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        assert _build_kubectl_global_args() == []

    def test_kubeconfig_explicit(self):
        result = _build_kubectl_global_args(kubeconfig="/path/to/kubeconfig")
        assert result == ["--kubeconfig", "/path/to/kubeconfig"]

    def test_context_settings_only(self, monkeypatch):
        # K7: context is NOT a caller parameter — the runtime channel owns
        # connection identity (settings only).
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings, "kube_context", "my-context")
        assert _build_kubectl_global_args() == ["--context", "my-context"]

    def test_no_cluster_flag_ever(self, monkeypatch):
        # K7 (#51-R3): an LLM-supplied cluster name is always invalid under
        # the kubewiz/single-cluster lock — --cluster is never emitted and
        # the parameter no longer exists on any surface.
        import inspect
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        assert "cluster" not in inspect.signature(_build_kubectl_global_args).parameters
        assert _build_kubectl_global_args(kubeconfig="/path/kc") == ["--kubeconfig", "/path/kc"]

    def test_kubeconfig_param_plus_settings_context(self, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubeconfig_path", "")
        monkeypatch.setattr(_settings, "kube_context", "ctx")
        result = _build_kubectl_global_args(kubeconfig="/path/kc")
        assert result == ["--kubeconfig", "/path/kc", "--context", "ctx"]

    def test_tool_schemas_drop_connection_params(self):
        # K7: the LLM-facing surface no longer invites context/cluster
        # overrides — args_schema IS the tool schema the model sees.
        for tool in (kubectl, kubectl_read):
            fields = tool.args_schema.model_fields
            assert "context" not in fields
            assert "cluster" not in fields

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
        })
        cmd = mock_run_command.call_args[0][0]
        assert "nodes" in cmd

    async def test_get_with_label_selector(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -l app=my-app -o json",
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "-l" in cmd
        assert "app=my-app" in cmd

    async def test_get_with_field_selector(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default --field-selector=status.phase=Pending -o json",
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--field-selector=status.phase=Pending" in cmd

    async def test_kubeconfig_injected(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "/my/kubeconfig",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_context_injected_from_settings(self, mock_run_command, monkeypatch):
        # K7: --context is no longer a tool parameter — it comes from the
        # runtime channel (settings) at command-build time.
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_context", "prod-ctx")
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--context" in cmd
        assert "prod-ctx" in cmd

    async def test_failure_returns_error(self, mock_run_command_fail):
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
        })
        assert "Error" in result


class TestEmptySelectorHint:
    """Empty-set receipt generation for selector-bearing ``get`` calls.

    The hint is the framework-generated anchor the replan-review guard
    pairs probes against (``execute_loop._target_absence_proven_in_epoch``),
    so its generation conditions are a security contract, not just UX.
    Two empty-set forms must both anchor: server-side channels (kubewiz)
    return a literally empty stdout, while a local CLI (kubeconfig
    channel) prints the table printer's "No resources found..." line
    with exit 0 (kubernetes/kubectl#1596) — equally an empty match set.
    """

    async def _run_get(self, mocker, stdout, v_args):
        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]

        async def _mock_exec(cmd, *args, **kwargs):
            return CommandResult(
                exit_code=0, stdout=stdout, stderr="", duration_ms=1.0,
            )

        mocker.patch.object(kubectl_mod, "execute_via_transport", new=_mock_exec)
        return await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": v_args,
            "kubeconfig": "",
        })

    async def test_empty_stdout_with_selector_appends_hint(self, mocker):
        """Server-side channels: a literally empty stdout anchors."""
        out = await self._run_get(mocker, "", "pods -n demo -l app=foo")
        assert EMPTY_SELECTOR_HINT in out

    async def test_cli_no_resources_line_appends_hint(self, mocker):
        """Local CLI (kubeconfig channel): the table printer's empty-set
        line is exit-0 stdout, so without this form the hint — and the
        replan-review absence proof anchored on it — is dead code on
        direct connections."""
        out = await self._run_get(
            mocker,
            "No resources found in demo namespace.\n",
            "pods -n demo -l app=foo",
        )
        assert EMPTY_SELECTOR_HINT in out
        # The CLI line stays visible next to the hint.
        assert "No resources found in demo namespace." in out

    async def test_cli_no_resources_without_namespace_suffix(self, mocker):
        """Older kubectl variants print the line without a namespace
        suffix — startswith() must cover them."""
        out = await self._run_get(
            mocker, "No resources found.\n", "pods -n demo -l app=foo",
        )
        assert EMPTY_SELECTOR_HINT in out

    async def test_long_selector_form_appends_hint(self, mocker):
        """--selector (spaced long form) anchors like -l."""
        out = await self._run_get(
            mocker,
            "No resources found in demo namespace.\n",
            "pods -n demo --selector app=foo",
        )
        assert EMPTY_SELECTOR_HINT in out

    async def test_no_resources_without_selector_no_hint(self, mocker):
        """The empty-set line alone (no selector) carries no label-guidance
        semantics — no hint."""
        out = await self._run_get(
            mocker, "No resources found in demo namespace.\n", "pods -n demo",
        )
        assert EMPTY_SELECTOR_HINT not in out

    async def test_populated_output_no_hint(self, mocker):
        """A real match never triggers the empty-set hint."""
        out = await self._run_get(
            mocker,
            "NAME    READY   STATUS\npod-1   1/1     Running\n",
            "pods -n demo -l app=foo",
        )
        assert EMPTY_SELECTOR_HINT not in out


class TestKubectlDescribe:
    """Test kubectl tool with subcommand='describe'."""

    async def test_describe_with_namespace(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "describe",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "",
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
        })
        call_kwargs = mock_run_command.call_args[1]
        # exec subcommand should use timeout_kubectl_exec (600s by default
        # since the 2026-09-20 user ruling)
        assert call_kwargs.get("timeout") == 600

    async def test_exec_with_kubeconfig(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "/my/kubeconfig",
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

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- iptables -A INPUT -j DROP",
            "kubeconfig": "",
        })
        assert result.startswith("Error:")
        assert "Command timed out after 10s" in result
        assert "kubectl exec failed:" not in result

    async def test_tool_timeout_presents_unknown_outcome_reconcile_first(self, monkeypatch):
        # R57: a caller-budget expiry is outcome-UNKNOWN. The local kill does
        # not stop the remote command (measured live: a 70s task's marker
        # landed at t+72s after the 60s budget kill), and the bare "timed
        # out" text reads as SHORT_RETRY — a blind retry then double-executes
        # a side-effecting command while the first task is still running.
        # The ToolTimeoutError branch must keep the "Error:" contract and
        # the raw text (the transient budget still sees it) while appending
        # the reconcile-first guidance.
        from chaos_agent.errors import ErrorAction, ToolTimeoutError, classify_error

        async def fake_run(cmd, *args, **kwargs):
            raise ToolTimeoutError(
                "Command timed out after 60s: kubectl delete pod nx"
            )

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod nx",
            "kubeconfig": "",
        })
        # The failure-marker contract and the raw timeout text survive...
        assert result.startswith("Error: kubectl delete:")
        assert "Command timed out after 60s" in result
        assert "failed" not in result.split("\n")[0]
        # ...the classification stays SHORT_RETRY (the retry budget still
        # counts this shape — the presentation changed, not the class)...
        assert classify_error(result).action == ErrorAction.SHORT_RETRY
        # ...and the outcome-unknown guidance teaches reconcile-first.
        assert "STILL be running" in result
        assert "double-execute" in result

    async def test_receipt_timeout_appends_unknown_outcome(self, monkeypatch):
        # R59: the receipt-form sibling of the R57 branch. The caller timeout
        # feeds both the local kill and the wiz CLI's --wait-timeout mirror;
        # when the CLI wait expires first, the CLI exits non-zero with the
        # platform's fixed "task timed out" receipt (measured verbatim in
        # R56) and parse_wiz_output passes it through — no ToolTimeoutError,
        # so the R57 exception branch never sees it. The raw receipt must
        # survive and gain the reconcile-first guidance.
        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(
                1,
                "",
                "Error: task timed out after 30s (task_uuid: u-1)",
                1.0,
            )

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods",
            "kubeconfig": "",
        })
        # Raw receipt verbatim, unchanged head, no "failed" verdict...
        assert result.startswith("Error: kubectl get (exit 1):")
        assert "task timed out after 30s" in result
        assert "failed" not in result.split("\n")[0]
        from chaos_agent.errors import ErrorAction, classify_error
        assert classify_error(result).action == ErrorAction.SHORT_RETRY
        # ...plus the outcome-unknown reconcile-first guidance.
        assert "Outcome UNKNOWN" in result
        assert "STILL be running" in result
        assert "double-execute" in result

    async def test_exec_nonzero_exit_reports_code_and_raw_output(self, mock_run_command_fail):
        # Non-zero exit surfaces the exit code + raw stderr verbatim, without a
        # "failed" verdict word (the raw output speaks; the LLM judges).
        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "",
        })
        assert result.startswith("Error:")
        assert "(exit 1)" in result
        assert "command failed" in result  # raw stderr preserved
        assert "kubectl exec failed:" not in result

    @pytest.mark.asyncio
    async def test_exec_completed_debug_pod_gets_keepalive_hint(self, monkeypatch):
        """#31/B38: exec against a COMPLETED node-debugger pod must point at
        the fix. The model chose a short keep-alive (``-- sleep 60``); the pod
        SUCCEEDED mid-probing and every later exec hit a bare "cannot exec
        into a container in a completed pod". The documented convention is
        ``-- sleep 3600``; the error must name it instead of leaving the
        generic reminder loop to chew on the raw text."""
        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(
                1,
                "",
                "error: cannot exec into a container in a completed pod; "
                "current phase is Succeeded",
                1.0,
            )

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "node-debugger-node-a-x1 -n default -- chroot /host which systemd-run",
            "kubeconfig": "",
        })
        assert result.startswith("Error:")
        assert "cannot exec into a container in a completed pod" in result
        assert "keep-alive" in result
        assert "sleep 3600" in result
        assert "Recreate the debug pod" in result

    @pytest.mark.asyncio
    async def test_exec_completed_workload_pod_gets_no_keepalive_hint(self, monkeypatch):
        """B38 scope guard: a completed WORKLOAD pod is business semantics —
        the keep-alive hint must stay scoped to node-debugger probe pods."""
        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(
                1,
                "",
                "error: cannot exec into a container in a completed pod; "
                "current phase is Succeeded",
                1.0,
            )

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-job-pod -n default -- cat /tmp/result",
            "kubeconfig": "",
        })
        assert result.startswith("Error:")
        assert "cannot exec into a container in a completed pod" in result
        assert "keep-alive" not in result

    @pytest.mark.asyncio
    async def test_debug_help_returns_help_text_not_parse_error(self, monkeypatch):
        """#30 msg 18: `debug --help` is a documentation request the tools'
        own guidance teaches ("Unknown-flag error → --help"). Routing it
        through the debug-pod name parse yielded the misleading "create
        may never have executed / Do NOT retry" error on top of the help
        text — one wasted digestion round. The help receipt must return
        the raw help output unchanged."""
        help_text = (
            "Debug cluster resources using interactive container.\n\n"
            "Usage:\n  kubectl debug RESOURCE/NAME [options]\n"
            "  kubectl debug node/NAME -it --image=busybox\n\n"
            "Options:\n  --profile=''\n  --image=''\n"
        )

        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(0, help_text, "", 0.5)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "--help",
            "kubeconfig": "",
        })
        # The help text passes through verbatim...
        assert "Debug cluster resources" in result
        assert "Usage:" in result
        # ...and the pod-parse failure diagnosis never fires.
        assert "no debug pod name" not in result
        assert "may never have executed" not in result
        assert "Do NOT retry" not in result


class TestKubectlExecPayloadIntegrity:
    r"""inject-17617837: the old string-level selector-strip regex
    `(?:^|\s)(-l|--selector)\s+\S+` ran on the RAW v_args and could not see
    quote regions — it amputated `` -l /proc/$(cat`` from inside a quoted
    ``sh -c '...'`` exec payload (``ls -l /proc/$(cat ...)``), and the
    container received a syntax error. Flag-shaped text past ``--`` is
    payload, never a kubectl flag. These tests pin the tokenized two-layer
    contract: hygiene only before ``--``, payload verbatim after it, and
    kubectl-level selectors on exec rejected BEFORE dispatch (never
    silently rewritten)."""

    async def test_msg60_regression_quoted_payload_verbatim(self, mock_run_command):
        # The exact v_args shape from the incident (msg[60]): every byte of
        # the quoted sh -c payload must reach dispatch untouched.
        v_args = (
            "zookeeper-0-0 -n taokeeper -- sh -c 'cat /tmp/memcache-warmup.pid "
            "2>/dev/null && echo \"---\" && ls -l /proc/$(cat "
            "/tmp/memcache-warmup.pid)/exe 2>/dev/null'"
        )
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": v_args,
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        payload = " ".join(cmd[cmd.index("sh") :])
        assert "ls -l /proc/$(cat /tmp/memcache-warmup.pid)/exe" in payload
        # The amputation signature must be absent — this exact fragment is
        # what the container received in the incident.
        assert "ls /tmp/memcache-warmup.pid)/exe" not in payload

    async def test_payload_ls_dash_l_survives(self, mock_run_command):
        # Skill recipe shape (DNS劫持 case): `ls -l` INSIDE the payload is a
        # flag of ls, not a kubectl selector — must never be touched.
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- sh -c 'ls -l /etc/hosts; id -u'",
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "ls -l /etc/hosts; id -u" in cmd

    async def test_payload_wc_dash_l_survives(self, mock_run_command):
        # `wc -l` amputated to `wc` HANGS waiting on stdin — worse than a
        # syntax error. Pin the full token.
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- sh -c 'wc -l /var/log/app.log'",
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "wc -l /var/log/app.log" in cmd

    async def test_kubectl_level_selector_rejected_before_dispatch(self, mock_run_command):
        # A genuine kubectl-level -l on exec is rejected with reason + fix,
        # BEFORE any dispatch — never silently rewritten.
        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -l app=nginx -- ls",
            "kubeconfig": "",
        })
        mock_run_command.assert_not_called()
        assert result.startswith("Error: kubectl exec does not support -l/--selector")
        assert "kubectl get pods" in result  # fix guidance present

    async def test_kubectl_level_long_selector_rejected(self, mock_run_command):
        result = await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default --selector app=nginx -- ls",
            "kubeconfig": "",
        })
        mock_run_command.assert_not_called()
        assert result.startswith("Error: kubectl exec does not support -l/--selector")

    async def test_get_selector_untouched(self, mock_run_command):
        # `-l` on `get` is legitimate — the reject applies to exec only.
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -l app=nginx",
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "-l" in cmd and "app=nginx" in cmd
        mock_run_command.assert_called_once()

    async def test_kubeconfig_inside_payload_survives(self, mock_run_command):
        # A --kubeconfig past "--" belongs to a NESTED kubectl inside the
        # payload — kubectl-layer stripping must stop at the separator.
        v_args = (
            "my-pod -n default -- sh -c 'kubectl get pods "
            "--kubeconfig /etc/nested/kubeconfig -o name'"
        )
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": v_args,
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        joined = " ".join(cmd)
        assert "/etc/nested/kubeconfig" in joined


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

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a --image=local/debug -- sleep 900",
            "kubeconfig": "",
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

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns --image=bad/image -- sleep 900",
            "kubeconfig": "",
        })

        assert result.startswith("Error:")
        assert "ImagePullBackOff" in result
        assert '"namespace":"test-ns"' in result
        assert '"ready":false' in result
        assert '"cleaned":true' in result
        assert "Do NOT call kubectl exec" in result
        assert "cleaned up automatically" in result

    @pytest.mark.asyncio
    async def test_copy_mode_debug_is_tracked_as_a_created_pod(self, monkeypatch):
        # ``--copy-to`` is documented as "Create a copy of the target Pod with
        # this name": it produces a NEW tool-owned pod and prints its name, so it
        # must keep the created-pod lifecycle (wait for Ready, register, allow
        # cleanup). Routing it through the ephemeral-container path would hunt
        # for containers that never appear and leave the copy untracked —
        # a leaked pod, and with ``--replace`` the original is deleted too.
        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " config " in f" {command} ":
                return CommandResult(0, "ns", "", 1.0)
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod p0-dbg with container debugger "
                    "on pod p0.",
                    "", 1.0,
                )
            if " wait " in f" {command} ":
                return CommandResult(0, "pod/p0-dbg condition met", "", 1.0)
            return CommandResult(0, json.dumps({
                "metadata": {"name": "p0-dbg", "namespace": "ns", "uid": "uid-c"},
                "spec": {
                    "nodeName": "node-a",
                    "containers": [{"securityContext": {"privileged": True}}],
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{"ready": True, "state": {}}],
                },
            }), "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "p0 -n ns --image=img --copy-to=p0-dbg -- sleep 3600",
            "kubeconfig": "",
        })

        # The copy is tracked as a created pod, with a cleanup handle.
        assert '"name":"p0-dbg"' in result
        assert '"ready":true' in result
        assert "subcommand='delete'" in result
        # Not mistaken for an ephemeral container attachment.
        assert "ephemeral_container" not in result
        assert "attached no ephemeral container" not in result

    @pytest.mark.asyncio
    async def test_pod_scoped_debug_resolves_ephemeral_container(self, monkeypatch):
        # Pod-scoped ``kubectl debug <pod> --target=`` attaches an ephemeral
        # container; kubectl prints no name (only "Targeting container ...").
        # The tool must resolve the name from ephemeralContainerStatuses, report
        # the exec handle, and NEVER delete the target pod. Regression for
        # task-3a360709 [139] where this looped as "created no identifiable pod".
        calls = []
        seen_debug = [False]

        async def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            command = " ".join(cmd)
            if " config " in f" {command} ":
                return CommandResult(0, "arms-prom", "", 1.0)
            if " debug " in f" {command} ":
                seen_debug[0] = True
                return CommandResult(
                    0,
                    'Targeting container "app". If you don\'t see processes '
                    "from this container it may be because the container "
                    "runtime doesn't support this feature.\n",
                    "", 1.0,
                )
            # get pod -o json: BEFORE dispatch (snapshot) the pod has no
            # ephemeral container yet; after dispatch the created one shows up.
            if not seen_debug[0]:
                return CommandResult(0, json.dumps({
                    "metadata": {"name": "p0", "namespace": "arms-prom", "uid": "u-9"},
                    "spec": {"nodeName": "node-a"},
                    "status": {"phase": "Running"},
                }), "", 1.0)
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

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": ("p0 -n arms-prom --image=img --target=app "
                       "--profile=netadmin --quiet -- sleep 1800"),
            "kubeconfig": "",
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
        seen_debug = [False]

        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " config " in f" {command} ":
                return CommandResult(0, "arms-prom", "", 1.0)
            if " debug " in f" {command} ":
                seen_debug[0] = True
                return CommandResult(0, 'Targeting container "app".\n', "", 1.0)
            # pre-dispatch snapshot: no ephemeral containers yet
            if not seen_debug[0]:
                return CommandResult(0, json.dumps({
                    "metadata": {"name": "p0", "namespace": "arms-prom", "uid": "u-9"},
                    "spec": {"nodeName": "node-a"},
                    "status": {"phase": "Running"},
                }), "", 1.0)
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

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
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
            "kubeconfig": "",
        })
        assert result.startswith("Error:")
        assert "debugger-xy12" in result
        assert "ImagePullBackOff" in result
        assert "Do NOT delete the target pod" in result


class TestKubectlDebugOneshot:
    """One-shot COMMAND mode (``debug ... -- CMD``) polls the terminal phase.

    The pod runs CMD once and terminates; condition=Ready is never true
    there, so the Ready-wait path would be a guaranteed false negative
    (task-29848471 k3 false alarm).
    """

    @staticmethod
    def _pod_json(phase: str, exit_code=None, reason: str = "") -> str:
        terminated = {}
        if exit_code is not None:
            terminated = {"exitCode": exit_code, "reason": reason or "Completed"}
        return json.dumps({
            "metadata": {
                "name": "node-debugger-node-a-x1",
                "namespace": "test-ns",
                "uid": "uid-debug-x1",
            },
            "spec": {"nodeName": "node-a"},
            "status": {
                "phase": phase,
                "containerStatuses": [
                    {"ready": False, "state": {"terminated": terminated} or {}}
                ],
            },
        })

    @pytest.mark.asyncio
    async def test_oneshot_succeeded_reports_exit_code_and_logs(self, monkeypatch):
        calls = []

        async def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            command = " ".join(cmd)
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod node-debugger-node-a-x1 "
                    "with container debugger on node node-a.",
                    "", 1.0,
                )
            if " logs " in f" {command} ":
                return CommandResult(0, "Filesystem  Size  Used\n/dev/vda1  40G  38G", "", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            return CommandResult(0, self._pod_json("Succeeded", exit_code=0), "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns --image=busybox -- df -h /host",
            "kubeconfig": "",
        })

        # No Ready wait in command mode — terminal polling only.
        assert not any("wait" in cmd for cmd in calls)
        assert not result.startswith("Error:")
        assert "exit_code=0" in result
        assert '"oneshot":true' in result
        assert '"cleaned":true' in result
        assert "Filesystem" in result  # logs tail surfaced
        assert "NOTHING to clean up" in result

    @pytest.mark.asyncio
    async def test_oneshot_nonzero_exit_neutral_not_error_prefixed(self, monkeypatch):
        """#31/B39: a non-zero one-shot exit is probe semantics, not failure.

        Existence/residue pre-checks report absence THROUGH non-zero exits
        (exit 2 "No such file", exit 4 "could not be found") — the skill
        corpus legislates that as the expected PASS form. The old "Error:"
        prefix also ignited the framework's RUNTIME EVIDENCE reminder, a
        digestion round the model paid before continuing.
        """
        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod node-debugger-node-a-x1 "
                    "with container debugger on node node-a.",
                    "", 1.0,
                )
            if " logs " in f" {command} ":
                return CommandResult(0, "df: /host/missing: No such file", "", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            return CommandResult(0, self._pod_json("Failed", exit_code=1), "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns --image=busybox -- df -h /host/missing",
            "kubeconfig": "",
        })

        # Neutral, not error-prefixed: the RUNTIME EVIDENCE reminder trigger
        # is content.startswith("Error") — this receipt must not light it.
        assert not result.startswith("Error:")
        assert "exit_code=1" in result
        assert "No such file" in result  # logs tail still surfaced verbatim
        assert '"cleaned":true' in result
        assert "not automatically a failure" in result  # judge-from-logs cue

    @staticmethod
    def _evicted_pod_json() -> str:
        # #29 verify (task inject-dbcf6112): a one-shot debug pod rejected
        # by disk-pressure admission NEVER STARTS its container —
        # containerStatuses is empty and the only failure signal lives in
        # the pod-level status.reason / status.message.
        return json.dumps({
            "metadata": {
                "name": "node-debugger-node-a-ndnzk",
                "namespace": "test-ns",
                "uid": "uid-debug-ndnzk",
            },
            "spec": {"nodeName": "node-a"},
            "status": {
                "phase": "Failed",
                "reason": "Evicted",
                "message": (
                    "The node was low on resource: ephemeral-storage. "
                    "Threshold quantity: 12254861926, available: 0."
                ),
                "containerStatuses": [],
            },
        })

    @pytest.mark.asyncio
    async def test_oneshot_pod_level_failure_reason_rides_the_meta(self, monkeypatch):
        """A pod rejected before its container starts (taint/admission
        blocks, #29 verify) reports phase=Failed with EMPTY container
        fields — the meta must carry pod-level status.reason/message so
        the model can attribute the failure without a node-conditions +
        events round trip (#29 paid one, ~45s)."""
        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod node-debugger-node-a-ndnzk "
                    "with container debugger on node node-a.",
                    "", 1.0,
                )
            if " logs " in f" {command} ":
                return CommandResult(0, "", "", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            return CommandResult(0, self._evicted_pod_json(), "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns --image=busybox -- df -h /host",
            "kubeconfig": "",
        })

        # The Failed pod without an exitCode lands as neutral exit 1...
        assert "exit_code=1" in result
        assert not result.startswith("Error:")
        # ...with the pod-level attribution now visible in the meta.
        assert '"reason":"Evicted"' in result
        assert "ephemeral-storage" in result
        # Container-level fields stay empty-and-reported (pre-start reject).
        assert '"exit_code":null' in result
        assert '"waiting_reasons":[]' in result

    @pytest.mark.asyncio
    async def test_oneshot_budget_expiry_points_to_systemd_carrier(self, monkeypatch):
        """Budget expiry must carry the fix direction (inject-59b289a6): a
        sustained loop hosted in a one-shot debug pod died at the 120s cap
        and the model had to self-diagnose the carrier mistake (~150s +
        fragmented fault window). The error itself must name the carrier."""

        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod node-debugger-node-a-x1 "
                    "with container debugger on node node-a.",
                    "", 1.0,
                )
            if " logs " in f" {command} ":
                return CommandResult(0, "", "", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            # Pod never terminates — forces the budget-expiry path.
            return CommandResult(0, self._pod_json("Running"), "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        monkeypatch.setattr(kubectl_mod.settings, "timeout_kubectl_exec", 1)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns --image=busybox -- df -h /host",
            "kubeconfig": "",
        })

        assert result.startswith("Error:")
        assert "did not terminate within" in result
        assert '"cleaned":true' in result
        # Fix direction: long payloads belong in a host systemd service,
        # not in a probe pod that the wrapper cleans on budget expiry.
        assert "systemd" in result

    def test_sleep_placeholder_stays_interactive(self):
        """``-- sleep N`` is the documented keep-alive convention: the pod
        must go through the Ready wait, never the terminal poll."""
        from chaos_agent.tools.kubectl_cli import _debug_has_oneshot_command
        assert _debug_has_oneshot_command(
            ["node/node-a", "--image=busybox", "--", "sleep", "3600"]
        ) is False
        assert _debug_has_oneshot_command(
            ["node/node-a", "--image=busybox", "--", "df", "-h"]
        ) is True
        # Interactive flags and bare/missing ``--`` stay interactive too.
        assert _debug_has_oneshot_command(
            ["-it", "node/node-a", "--", "df"]
        ) is False
        assert _debug_has_oneshot_command(["node/node-a", "--"]) is False
        assert _debug_has_oneshot_command(["node/node-a"]) is False

    @pytest.mark.asyncio
    async def test_unparseable_output_warns_against_blind_retry(self, monkeypatch):
        """Exit 0 + no identifiable pod: the create may never have executed.
        The text must NOT invite retrying the same command (k3 burned three
        rounds doing exactly that)."""
        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " config " in f" {command} ":
                return CommandResult(0, "test-ns", "", 1.0)
            return CommandResult(0, 'Warning: some unusual output', "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        # Discovery fallback also hits _debug_pod's own transport binding;
        # make it return non-JSON so discovery misses deterministically.
        import chaos_agent.agent.nodes.execute._debug_pod as debug_pod_mod
        monkeypatch.setattr(debug_pod_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a --image=busybox -- df -h /host",
            "kubeconfig": "",
        })

        assert result.startswith("Error:")
        assert "may never have executed" in result
        assert "Do NOT retry" in result
        assert "Warning: some unusual output" in result

    @pytest.mark.asyncio
    async def test_parse_failure_discovery_hit_continues_flow(self, monkeypatch):
        """Warning-only stdout (no pod name) + live discovery hit: the tool
        must continue with the discovered pod instead of erroring out."""
        now_iso = __import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " debug " in f" {command} ":
                return CommandResult(0, "Warning: unusual output, no pod name", "", 1.0)
            if " get " in f" {command} " and "pods" in command and "-o json" in command:
                # Discovery list: only the fresh node-debugger pod qualifies.
                return CommandResult(0, json.dumps({"items": [
                    {
                        "metadata": {
                            "name": "node-debugger-node-a-x1",
                            "creationTimestamp": now_iso,
                        },
                        "spec": {"nodeName": "node-a"},
                    },
                ]}), "", 1.0)
            if " logs " in f" {command} ":
                return CommandResult(0, "disk usage 92%", "", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            return CommandResult(0, self._pod_json("Succeeded", exit_code=0), "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        # Discovery runs inside _debug_pod, which holds its own import of
        # execute_via_transport — patch that binding too.
        import chaos_agent.agent.nodes.execute._debug_pod as debug_pod_mod
        monkeypatch.setattr(debug_pod_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns --image=busybox -- df -h /host",
            "kubeconfig": "",
        })

        assert not result.startswith("Error:")
        assert "discovered" in result  # discovery hit is surfaced
        assert "exit_code=0" in result
        assert '"cleaned":true' in result


class TestKubectlPatch:
    """Test kubectl tool with subcommand='patch'."""

    async def test_json_patch(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "patch",
            "v_args": 'pod my-pod -n default --type=json -p \'[{"op":"add","path":"/metadata/finalizers","value":["chaos-test/block"]}]\'',
            "kubeconfig": "",
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
        })
        cmd = mock_run_command.call_args[0][0]
        assert "patch" in cmd
        assert "--type" not in cmd

    async def test_patch_with_kubeconfig(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "patch",
            "v_args": 'pod my-pod -n default --type=json -p \'[{"op":"remove","path":"/metadata/finalizers"}]\'',
            "kubeconfig": "/my/kubeconfig",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_failure_returns_error(self, mock_run_command_fail):
        result = await kubectl.ainvoke({
            "subcommand": "patch",
            "v_args": 'pod my-pod -n default -p \'{"metadata":{}}\'',
            "kubeconfig": "",
        })
        assert "Error" in result


class TestKubectlDelete:
    """Test kubectl tool with subcommand='delete'."""

    async def test_delete_pod_by_name(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "",
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
        })
        cmd = mock_run_command.call_args[0][0]
        assert "-l" in cmd
        assert "app=my-app" in cmd

    async def test_force_delete(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod my-pod -n default --force --grace-period=0",
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--force" in cmd
        assert "--grace-period=0" in cmd

    async def test_delete_with_kubeconfig(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "/my/kubeconfig",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_failure_returns_error(self, mock_run_command_fail):
        result = await kubectl.ainvoke({
            "subcommand": "delete",
            "v_args": "pod my-pod -n default",
            "kubeconfig": "",
        })
        assert "Error" in result


class TestKubectlScale:
    """Test kubectl tool with subcommand='scale'."""

    async def test_scale_deployment_by_name(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment my-deploy -n default --replicas=3",
            "kubeconfig": "",
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
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--replicas=0" in cmd

    async def test_scale_by_label_selector(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment -n default -l app=my-app --replicas=1",
            "kubeconfig": "",
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
        })
        cmd = mock_run_command.call_args[0][0]
        assert "--kubeconfig" in cmd
        assert "/my/kubeconfig" in cmd

    async def test_failure_returns_error(self, mock_run_command_fail):
        result = await kubectl.ainvoke({
            "subcommand": "scale",
            "v_args": "deployment my-deploy -n default --replicas=3",
            "kubeconfig": "",
        })
        assert "Error" in result


class TestKubectlCordonUncordon:
    """Test kubectl tool with subcommand='cordon'/'uncordon'."""

    async def test_cordon_node(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "cordon",
            "v_args": "my-node",
            "kubeconfig": "",
        })
        cmd = mock_run_command.call_args[0][0]
        assert "cordon" in cmd
        assert "my-node" in cmd

    async def test_uncordon_node(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "uncordon",
            "v_args": "my-node",
            "kubeconfig": "",
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
        })
        assert "LARGE_OUTPUT" in result

    async def test_small_json_output_no_hint(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubectl_max_output_bytes", 32768)
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
        })
        assert "LARGE_OUTPUT" not in result

    async def test_non_json_output_no_hint(self, mock_run_command, monkeypatch):
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kubectl_max_output_bytes", 1)
        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o wide",
            "kubeconfig": "",
        })
        assert "LARGE_OUTPUT" not in result

    async def test_valve_fired_skips_hint_and_keeps_ceiling(
        self, mock_run_command, monkeypatch,
    ):
        """Valve and LARGE_OUTPUT must not stack (truncation-governance audit).

        Over the 64KB safety-valve ceiling the valve fires; the hint must
        NOT append on top: its narrowing strategies already live in the
        valve's shared notice (kind "success-output"), its size figure
        would describe the post-valve text rather than the original, and
        ~300B of appended hint would push the returned message past the
        ceiling the valve just enforced.
        """
        from chaos_agent.config.settings import settings as _settings
        from chaos_agent.utils.truncation import TOOL_OUTPUT_SAFETY_VALVE_BYTES
        monkeypatch.setattr(_settings, "kubectl_max_output_bytes", 100)
        # ~324B per item x 220 items ≈ 71KB — comfortably over the 64KB
        # ceiling so the valve fires, and JSON-shaped so the hint gate
        # (get + -o json + over-budget) would fire without the guard.
        large_json = (
            '{"items": ['
            + ",".join(['{"kind": "Pod","pad":"' + "y" * 300 + '"}'] * 220)
            + "]}"
        )
        assert len(large_json.encode()) > TOOL_OUTPUT_SAFETY_VALVE_BYTES
        mock_run_command.side_effect = None
        mock_run_command.return_value = CommandResult(
            exit_code=0, stdout=large_json, stderr="", duration_ms=100.0,
        )

        result = await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
        })
        assert "⚠️ OUTPUT_TRUNCATED" in result
        assert "LARGE_OUTPUT" not in result
        # The valve's ceiling promise holds on the RETURNED message.
        assert len(result.encode("utf-8", errors="replace")) <= (
            TOOL_OUTPUT_SAFETY_VALVE_BYTES
        )


class TestKubectlTimeouts:
    """Test that exec subcommand uses longer timeout."""

    async def test_get_uses_default_timeout(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "pods -n default -o json",
            "kubeconfig": "",
        })
        call_kwargs = mock_run_command.call_args[1]
        from chaos_agent.config.settings import settings as _settings
        assert call_kwargs.get("timeout") == _settings.timeout_kubectl

    async def test_exec_uses_longer_timeout(self, mock_run_command):
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": "my-pod -n default -- ls",
            "kubeconfig": "",
        })
        call_kwargs = mock_run_command.call_args[1]
        assert call_kwargs.get("timeout") == 600


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

    def test_dquote_backslash_escapes_single_token(self):
        r"""#45 锚定（方案 A）：合法的 shell 双引号+反斜杠转义载荷必须到达
        容器为一个 argv token——修复前的分词器在第一个 ``\"`` 处假闭合，
        把载荷静默碎片化（无报错，唯 Agent 回显目检门可拦）。"""
        result = _split_args(
            r'exec deploy/app -- sh -c "echo \"name\":\$X \`date\`"'
        )
        assert result == [
            "exec", "deploy/app", "--", "sh", "-c",
            'echo "name":$X `date`',
        ]

    def test_dquote_backslash_line_continuation_dropped(self):
        r"""双引号区内的 ``\<newline>`` 是行连接：两个字符都丢弃（POSIX
        sh 语义），前后内容拼接为同一 token。"""
        result = _split_args('sh -c "echo a\\\nb"')
        assert result == ["sh", "-c", "echo ab"]

    def test_dquote_unknown_backslash_stays_verbatim(self):
        r"""转义集之外的 ``\x`` 保持双字符 verbatim（反斜杠是字面量）——
        边界钉定：防止未来的重构把未知转义误吃掉（此类载荷 intact）。"""
        result = _split_args(r'sh -c "echo a\qb"')
        assert result == ["sh", "-c", r"echo a\qb"]

    def test_jsonpath_range_with_spaces_single_token(self):
        """Task inject-c8cdd105: a {range}…{end} template containing spaces
        was word-split into fragments, and kubectl reported ``error parsing
        jsonpath {range, unclosed action``. The whole template must survive
        as ONE token (block-keyword pairing terminates the merge, so the
        trailing flag is not swallowed)."""
        result = _split_args(
            "get node n1 -o jsonpath={range .status.conditions[*]}"
            "{.type}={.status} {end} -n cms"
        )
        assert result == [
            "get", "node", "n1", "-o",
            "jsonpath={range .status.conditions[*]}{.type}={.status} {end}",
            "-n", "cms",
        ]

    def test_jsonpath_nested_quotes_preserved(self):
        """Task inject-c8cdd105: shlex stripped the inner quotes of
        ``[?(@.type=='MemoryPressure')]`` and kubectl reported
        ``unrecognized identifier MemoryPressure``. The OUTER delimiter
        pair is dropped (shell semantics) but inner quotes must survive."""
        result = _split_args(
            "get node n1 -o jsonpath='{.status.conditions"
            "[?(@.type=='MemoryPressure')].status}'"
        )
        assert result == [
            "get", "node", "n1", "-o",
            "jsonpath={.status.conditions[?(@.type=='MemoryPressure')].status}",
        ]

    def test_jsonpath_nested_quotes_unquoted_form(self):
        """Same template without the outer delimiter pair."""
        result = _split_args(
            "get node n1 -o jsonpath={.status.conditions"
            "[?(@.type=='MemoryPressure')].status}"
        )
        assert result == [
            "get", "node", "n1", "-o",
            "jsonpath={.status.conditions[?(@.type=='MemoryPressure')].status}",
        ]

    def test_go_template_range_single_token(self):
        """go-template block forms merge the same way."""
        result = _split_args(
            "get pods -o go-template={{range .items}}"
            "{{.metadata.name}}\n{{end}}"
        )
        assert result == [
            "get", "pods", "-o",
            "go-template={{range .items}}{{.metadata.name}}\n{{end}}",
        ]

    def test_template_output_flag_forms_untouched(self):
        """Non-template -o values and other key=value flags are unaffected."""
        assert _split_args("get node n1 -o=jsonpath={.metadata.name}") == [
            "get", "node", "n1", "-o=jsonpath={.metadata.name}",
        ]
        assert _split_args("get pods -o custom-columns=NAME:.metadata.name") == [
            "get", "pods", "-o", "custom-columns=NAME:.metadata.name",
        ]
        assert _split_args("get pods --field-selector=status.phase=Running") == [
            "get", "pods", "--field-selector=status.phase=Running",
        ]

    def test_unterminated_template_consumes_rest(self):
        """Fail-open: a truncated template is passed through whole so
        kubectl reports the real template error, not a fragment artifact."""
        result = _split_args(
            "get node n1 -o jsonpath={range .status.conditions[*]}{.type}"
        )
        assert result == [
            "get", "node", "n1", "-o",
            "jsonpath={range .status.conditions[*]}{.type}",
        ]


class TestKubectlJsonpathQuoting:
    """Test that kubectl tool correctly passes jsonpath args with shell quoting."""

    async def test_jsonpath_quoted_arg_stripped(self, mock_run_command):
        """jsonpath='{.spec.replicas}' should be passed as jsonpath={.spec.replicas}."""
        await kubectl.ainvoke({
            "subcommand": "get",
            "v_args": "deployments -n ns my-deploy -o jsonpath='{.spec.replicas}'",
            "kubeconfig": "",
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


class TestReviewFindings:
    """Reproduction tests for post-implementation review findings.

    Each test pins a suspected bug BEFORE the fix; it must fail on the
    buggy code and pass after the fix.
    """

    # ---- Suspect 1: _debug_target_node_name breaks when a value-flag
    # (-n/--namespace/--image space form) precedes the node token. The
    # flag's VALUE is misread as the first positional -> discovery
    # fallback silently disabled for such calls.
    @pytest.mark.parametrize("args, expected", [
        (["node/node-a"], "node-a"),
        (["node/node-a", "-n", "default"], "node-a"),
        (["-n", "default", "node/node-a"], "node-a"),
        (["--namespace", "default", "node/node-a"], "node-a"),
        (["--image", "busybox", "node/node-a"], "node-a"),
        (["--profile=sysadmin", "-n", "ns1", "node", "node-a"], "node-a"),
        (["some-workload-pod", "-n", "default"], ""),  # pod-scoped
    ])
    def test_debug_target_node_name_with_value_flags(self, args, expected):
        from chaos_agent.tools.kubectl_cli import _debug_target_node_name
        assert _debug_target_node_name(args) == expected

    # ---- Suspect 2: a Succeeded one-shot pod whose containerStatuses
    # are not (yet) populated yields exit_code=None and lands in the
    # ERROR branch ("failed with exit_code=None") instead of success.
    @pytest.mark.asyncio
    async def test_oneshot_succeeded_without_container_statuses(self, monkeypatch):
        pod_json = json.dumps({
            "metadata": {
                "name": "node-debugger-node-a-x1",
                "namespace": "test-ns",
                "uid": "uid-debug-x1",
            },
            "spec": {"nodeName": "node-a"},
            "status": {"phase": "Succeeded", "containerStatuses": []},
        })

        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod node-debugger-node-a-x1 "
                    "with container debugger on node node-a.",
                    "", 1.0,
                )
            if " logs " in f" {command} ":
                return CommandResult(0, "done", "", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            return CommandResult(0, pod_json, "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        result = await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns --image=busybox -- df -h",
            "kubeconfig": "",
        })
        assert not result.startswith("Error:"), result
        assert "exit_code=0" in result

    # ---- Suspect 3: documented keep-alive variants wrapped in a shell
    # (`sh -c 'sleep 3600'`) or absolute-path sleep (`/bin/sleep`) are
    # misclassified as one-shot -> the carrier pod gets killed after a
    # 120s wait even though the caller expects to exec into it.
    @pytest.mark.parametrize("args, expected", [
        (["node/a", "--", "sleep", "3600"], False),          # documented
        (["node/a", "--", "/bin/sleep", "3600"], False),      # abs path
        (["node/a", "--", "sh", "-c", "sleep 3600"], False),  # wrapped
        (["node/a", "--", "bash", "-c", "sleep 60"], False),  # wrapped
        (["node/a", "--", "sh", "-c", "sleep 30 && df -h"], True),  # composite
        (["node/a", "--", "chroot", "/host", "crictl", "stop"], True),
    ])
    def test_sleep_keepalive_variants_stay_interactive(self, args, expected):
        from chaos_agent.tools.kubectl_cli import _debug_has_oneshot_command
        assert _debug_has_oneshot_command(args) is expected

    # ---- Suspect 4 (R45): the one-shot classifier's boundary is the first
    # standalone ``--``, not pflag's TRUE separator — a ``--`` in a
    # value-taking flag's value slot (``-c --`` / ``--image --`` /
    # ``--profile-output --``) starts the command view INSIDE the flag
    # value and the keep-alive shape reads as "not keep-alive": the
    # one-shot arm then deletes the pod after its terminal poll — a live
    # carrier killed. Measured red on the pre-fix code: rows 1-3 and 5
    # returned the wrong side (the legacy slice saw ``-- sleep 3600``).
    @pytest.mark.parametrize("args, expected", [
        # value-slot ``--``: the SECOND ``--`` is the boundary and the
        # command is ``sleep 3600`` — keep-alive.
        (["node/a", "-c", "--", "--", "sleep", "3600"], False),
        (["node/a", "--image", "--", "--", "sleep", "3600"], False),
        (["node/a", "--profile-output", "--", "--", "sleep", "3600"], False),
        # ...and a TRUE one-shot after a value slot stays one-shot
        # (control: correct before and after).
        (["node/a", "--image", "--", "--", "df", "-h"], True),
        # an interactive flag sitting AFTER the value slot is still seen —
        # the legacy scan stopped at the value slot and missed it.
        (["node/a", "--image", "--", "-it", "--", "sleep", "3600"], False),
        # no TRUE separator (every dash swallowed by ``-c``): the legacy
        # boundary is kept — command ``sleep 3600``, keep-alive, unchanged.
        (["node/a", "-c", "--", "sleep", "3600"], False),
    ])
    def test_oneshot_boundary_is_the_true_separator(self, args, expected):
        from chaos_agent.tools.kubectl_cli import _debug_has_oneshot_command
        assert _debug_has_oneshot_command(args) is expected

    # ---- Suspect 5 (R45): the v_args hygiene boundary (embedded
    # ``--kubeconfig/--context/--cluster`` stripping) is the first
    # standalone ``--`` — with a value-slot ``--`` the flag-shaped text
    # between it and the TRUE separator fell OUTSIDE the hygiene window
    # and reached kubectl as a real flag, letting payload text flip the
    # connection identity (K7: the runtime channel owns it). Measured red
    # on the pre-fix code: the argv handed to the transport carried
    # ``--context=prod``.
    @pytest.mark.asyncio
    async def test_value_slot_boundary_keeps_hygiene_window(self, monkeypatch):
        pod_json = json.dumps({
            "metadata": {
                "name": "node-debugger-node-a-x1",
                "namespace": "test-ns",
                "uid": "uid-debug-x1",
            },
            "spec": {"nodeName": "node-a"},
            "status": {"phase": "Succeeded", "containerStatuses": []},
        })
        captured: list[list[str]] = []

        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            captured.append(list(cmd))
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod node-debugger-node-a-x1 "
                    "with container debugger on node node-a.",
                    "", 1.0,
                )
            if " logs " in f" {command} ":
                return CommandResult(0, "done", "", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            return CommandResult(0, pod_json, "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -n test-ns -c -- --context=prod -- df -h",
            "kubeconfig": "",
        })
        debug_cmd = next(
            c for c in captured if " debug " in f" {' '.join(c)} "
        )
        assert "--context=prod" not in debug_cmd
        assert "df" in debug_cmd

    # ---- Suspect 6 (R45): the auto ``-n <ns>`` injection for debug goes
    # in before the first standalone ``--`` — with a value-slot ``--`` the
    # pair landed INSIDE the flag region and broke the ``-c``/value
    # pairing (the container flag then consumed ``-n``). The injection
    # must sit directly before the TRUE separator. Measured red on the
    # pre-fix code: the emitted argv had ``-c -n <ns>``.
    @pytest.mark.asyncio
    async def test_debug_namespace_injected_before_true_separator(
        self, monkeypatch,
    ):
        pod_json = json.dumps({
            "metadata": {
                "name": "node-debugger-node-a-x1",
                "namespace": "test-ns",
                "uid": "uid-debug-x1",
            },
            "spec": {"nodeName": "node-a"},
            "status": {"phase": "Succeeded", "containerStatuses": []},
        })
        captured: list[list[str]] = []

        async def fake_run(cmd, *args, **kwargs):
            command = " ".join(cmd)
            captured.append(list(cmd))
            if " debug " in f" {command} ":
                return CommandResult(
                    0,
                    "Creating debugging pod node-debugger-node-a-x1 "
                    "with container debugger on node node-a.",
                    "", 1.0,
                )
            if " logs " in f" {command} ":
                return CommandResult(0, "done", "", 1.0)
            if " delete " in f" {command} ":
                return CommandResult(0, "pod deleted", "", 1.0)
            return CommandResult(0, pod_json, "", 1.0)

        async def fake_namespace(_kubeconfig):
            return "test-ns"

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        monkeypatch.setattr(
            kubectl_mod, "_resolve_effective_namespace", fake_namespace,
        )

        await kubectl.ainvoke({
            "subcommand": "debug",
            "v_args": "node/node-a -c -- -- df -h",
            "kubeconfig": "",
        })
        debug_cmd = next(
            c for c in captured if " debug " in f" {' '.join(c)} "
        )
        # The ``-c`` value slot keeps its ``--`` adjacency; the injected
        # pair sits right before the TRUE separator.
        at_c = debug_cmd.index("-c")
        assert debug_cmd[at_c + 1] == "--"
        assert debug_cmd[at_c + 2: at_c + 5] == ["-n", "test-ns", "--"]

    # ---- Suspect 7 (R46): the pod/node target parsers read flag arity
    # from hand-written PARTIAL copies of the shared table (12 and 7 items
    # against the 40 in ``_readonly_facts``) — a global value flag
    # (``--request-timeout 30s`` / ``-v 6`` / ``--as admin`` /
    # ``--profile-output out.json`` / ``--cache-dir``) was not skipped and
    # its VALUE became the "first positional". For
    # ``_debug_target_pod_name`` that flips the SAFETY boundary: a
    # node-scoped call (``debug --request-timeout 30s node/n1 -- sleep
    # 3600``) reads as pod-scoped, the ephemeral-container arm runs and
    # errors naming a pod that does not exist, and the created
    # node-debugger pod (reported through neither that arm nor
    # ``[debug-pod-meta]``) leaks unregistered. For
    # ``_debug_target_node_name`` the discovery fallback is silently
    # disabled. Measured red on the pre-fix code: the value came back.
    @pytest.mark.parametrize("args, expected", [
        (["p0", "--image=busybox", "--", "df"], "p0"),
        (["--request-timeout", "30s", "p0", "--", "df"], "p0"),
        (["-v", "6", "p0", "--", "df"], "p0"),
        (["--as", "admin", "p0", "--", "df"], "p0"),
        (["--cache-dir", "/tmp/kc", "p0", "--", "df"], "p0"),
        (["--profile-output", "out.json", "p0", "--", "df"], "p0"),
        # node-scoped: the leaked-pod shape must stay empty (node scope)
        (["--profile-output", "out.json", "node/n1", "--", "sleep", "3600"], ""),
        (["--request-timeout", "30s", "node/n1", "--", "sleep", "3600"], ""),
        # controls that must not change
        (["--copy-to", "newp", "p0", "--", "df"], ""),
        (["-it", "p0", "--", "df"], "p0"),
    ])
    def test_debug_target_pod_name_skips_global_value_flags(self, args, expected):
        from chaos_agent.tools.kubectl_cli import _debug_target_pod_name
        assert _debug_target_pod_name(args) == expected

    @pytest.mark.parametrize("args, expected", [
        (["-n", "default", "node/n1", "--", "sleep", "3600"], "n1"),
        (["--profile-output", "out.json", "node/n1", "--", "sleep", "3600"], "n1"),
        (["--request-timeout", "30s", "node/n1", "--", "sleep", "3600"], "n1"),
        (["-v", "6", "node/n1", "--", "sleep", "3600"], "n1"),
        (["--as", "admin", "node/n1", "--", "sleep", "3600"], "n1"),
    ])
    def test_debug_target_node_name_skips_global_value_flags(self, args, expected):
        from chaos_agent.tools.kubectl_cli import _debug_target_node_name
        assert _debug_target_node_name(args) == expected

    # ---- Suspect 8 (R46): ``_namespace_from_args`` stops at the first
    # standalone ``--`` — a ``--`` in a value-taking flag's value slot
    # (``--profile-output --``) is that flag's VALUE, and an explicit ``-n``
    # after it was silently dropped; the tool then injected the resolved
    # namespace, which OVERRODE the explicit one (the last ``-n`` wins
    # under pflag) — namespace intent flipped. Measured red on the pre-fix
    # code: the scan returned "". A line with no TRUE separator keeps the
    # legacy boundary (pinned by the third case).
    def test_namespace_scan_uses_the_true_separator(self):
        from chaos_agent.tools.kubectl_cli import _namespace_from_args
        assert _namespace_from_args(
            ["node/n1", "--profile-output", "--", "-n", "test-ns", "--", "sleep"]
        ) == "test-ns"
        # control: a payload ``-n`` past the TRUE separator stays invisible
        assert _namespace_from_args(["p0", "--", "-n", "payload-ns"]) == ""
        # control: no TRUE separator → legacy boundary, unchanged
        assert _namespace_from_args(["p0", "-c", "--", "-n", "x"]) == ""


class TestExecBladeCreateTimeoutGuard:
    """Duration guard for ``kubectl exec ... blade create``.

    The blade_create tool path normalizes ``--timeout`` via
    ``normalize_timeout_flag`` (space AND equals form) before applying the
    minimum-duration policy. The exec-carried blade create must enforce the
    same guarantee: ChaosBlade legitimately accepts ``--timeout=30``
    (equals form), and a guard that only recognises the space form lets a
    too-short duration evade the boost — the fault auto-recovers before
    Layer1/Layer2 verification finishes.
    """

    @pytest.fixture(autouse=True)
    def _isolate_experiment_timeout(self):
        # Pin the operator default to the code floor so the
        # auto-injected value does not depend on the host machine's
        # ~/.blade-ai/config.json (a stale experiment_timeout there
        # would win in the unspecified-duration path).
        from chaos_agent.config.settings import blade_ai_context
        from chaos_agent.utils.fault_type import _DEFAULT_MIN_DURATION

        with blade_ai_context(experiment_timeout=_DEFAULT_MIN_DURATION):
            yield

    async def _invoke_and_capture(self, monkeypatch, v_args):
        captured = {}

        async def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = list(cmd)
            return CommandResult(0, '{"code":200,"result":"uid-1"}', "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        await kubectl.ainvoke({
            "subcommand": "exec",
            "v_args": v_args,
            "kubeconfig": "",
        })
        return captured["cmd"]

    @staticmethod
    def _timeout_pair(cmd):
        """Return the value following ``--timeout`` in cmd, or None."""
        for i, token in enumerate(cmd):
            if token == "--timeout" and i + 1 < len(cmd):
                return cmd[i + 1]
        return None

    @pytest.mark.asyncio
    async def test_missing_timeout_auto_injected(self, monkeypatch):
        cmd = await self._invoke_and_capture(
            monkeypatch,
            "tool-pod -n chaosblade -- blade create k8s pod-cpu fullload --cpu-percent 80",
        )
        assert self._timeout_pair(cmd) == "300"

    @pytest.mark.asyncio
    async def test_space_form_below_min_preserved(self, monkeypatch):
        # Explicit durations are honoured verbatim (l4-contract-faithfulness):
        # 30s stays 30s; the executor no longer lifts contract-stated values.
        cmd = await self._invoke_and_capture(
            monkeypatch,
            "tool-pod -n chaosblade -- blade create k8s pod-cpu fullload --timeout 30",
        )
        assert self._timeout_pair(cmd) == "30"

    @pytest.mark.asyncio
    async def test_equals_form_below_min_preserved(self, monkeypatch):
        # The equals form still normalizes to the canonical space form
        # (that guard stays); only the value is no longer boosted.
        cmd = await self._invoke_and_capture(
            monkeypatch,
            "tool-pod -n chaosblade -- blade create k8s pod-cpu fullload --timeout=30",
        )
        assert "--timeout=30" not in cmd
        assert self._timeout_pair(cmd) == "30"

    @pytest.mark.asyncio
    async def test_equals_form_above_min_preserved(self, monkeypatch):
        cmd = await self._invoke_and_capture(
            monkeypatch,
            "tool-pod -n chaosblade -- blade create k8s pod-cpu fullload --timeout=900",
        )
        assert self._timeout_pair(cmd) == "900"

    @pytest.mark.asyncio
    async def test_duplicate_timeout_last_wins(self, monkeypatch):
        # Mirrors blade_create semantics: keep the last explicit value,
        # canonicalize to a single space-form pair.
        cmd = await self._invoke_and_capture(
            monkeypatch,
            "tool-pod -n chaosblade -- blade create k8s pod-cpu fullload "
            "--timeout=30 --timeout 900",
        )
        assert cmd.count("--timeout") == 1
        assert self._timeout_pair(cmd) == "900"


class TestErrorOutputMergesBothStreams:
    """On non-zero exit both stdout and stderr carry evidence; an ``or``
    drops one side. task-inject-774ecd39's jsonpath call rendered a
    half-baked template on stdout while the real error sat on stderr —
    the model saw only the template and had to self-repair blind."""

    @pytest.mark.asyncio
    async def test_stdout_and_stderr_both_survive(self, monkeypatch):
        async def fake_run(cmd, *a, **kw):
            return CommandResult(
                1, "capacity={.status.capacity}",
                'error: error parsing jsonpath: unterminated "', 1.0,
            )

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        out = await kubectl.ainvoke({"subcommand": "get", "v_args": "node n1"})
        assert "exit 1" in out
        assert "capacity={.status.capacity}" in out      # stdout kept
        assert "error parsing jsonpath" in out            # stderr kept

    @pytest.mark.asyncio
    async def test_empty_streams_report_no_output(self, monkeypatch):
        async def fake_run(cmd, *a, **kw):
            return CommandResult(1, "", "", 1.0)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        out = await kubectl.ainvoke({"subcommand": "get", "v_args": "node n1"})
        assert "(no output)" in out


def _pod_json(spec_ec_names: list[str], status_entries: dict[str, dict]) -> str:
    """Target pod JSON shape: spec order = creation order; status entries are
    name -> state dict (the API itself returns the status list alphabetically,
    so callers must never rank by it)."""
    return json.dumps({
        "metadata": {"name": "tgt", "namespace": "default", "uid": "u-1"},
        "spec": {
            "nodeName": "node-a",
            "ephemeralContainers": [
                {"name": n, "securityContext": {}} for n in spec_ec_names
            ],
        },
        "status": {
            "phase": "Running",
            "ephemeralContainerStatuses": [
                {"name": n, "state": st}
                for n, st in sorted(status_entries.items())
            ],
        },
    })


class TestEphemeralContainerAttribution:
    """The created ephemeral container must be attributed by CREATION order
    (spec + pre-dispatch snapshot), never by the ALPHABETICAL status list.
    Cluster evidence: spec ``...z4gl7``-newest vs status ending ``...z4gl7``
    alphabetically made the wait report a stale container as 'did not start'.
    """

    def test_parse_prefers_spec_order_over_alphabetical_status(self):
        from chaos_agent.tools.kubectl_cli import _parse_ephemeral_container_name
        pod = _pod_json(
            ["debugger-crzj6", "debugger-5wzdw", "debugger-j6chx"],
            {
                "debugger-5wzdw": {"terminated": {"exitCode": 0}},
                "debugger-crzj6": {"terminated": {"exitCode": 2}},
                # 'z' sorts last — a stale container the old code picked
                "debugger-z4gl7": {"terminated": {"exitCode": 0}},
                "debugger-j6chx": {"running": {}},
            },
        )
        assert _parse_ephemeral_container_name(pod) == "debugger-j6chx"

    def test_parse_falls_back_to_status_without_spec(self):
        from chaos_agent.tools.kubectl_cli import _parse_ephemeral_container_name
        pod = json.dumps({
            "spec": {},
            "status": {"ephemeralContainerStatuses": [
                {"name": "debugger-aaa", "state": {}},
                {"name": "debugger-zzz", "state": {}},
            ]},
        })
        assert _parse_ephemeral_container_name(pod) == "debugger-zzz"

    def test_select_created_diffs_pre_dispatch_snapshot(self):
        from chaos_agent.tools.kubectl_cli import _select_created_ephemeral
        pod = _pod_json(
            ["debugger-old", "debugger-new"],
            {"debugger-new": {"running": {}}, "debugger-old": {"terminated": {"exitCode": 0}}},
        )
        assert _select_created_ephemeral(pod, {"debugger-old"}) == (
            "debugger-new", True,
        )

    def test_select_created_ignores_alphabetically_last_stale(self):
        from chaos_agent.tools.kubectl_cli import _select_created_ephemeral
        # stale 'z' container sorts last in status; the NEW one is mid-alphabet
        pod = _pod_json(
            ["debugger-stale", "debugger-zzz-stale", "debugger-mid"],
            {
                "debugger-mid": {"running": {}},
                "debugger-stale": {"terminated": {"exitCode": 0}},
                "debugger-zzz-stale": {"terminated": {"exitCode": 0}},
            },
        )
        assert (
            _select_created_ephemeral(pod, {"debugger-stale", "debugger-zzz-stale"})
            == ("debugger-mid", True)
        )

    def test_select_created_unconfirmed_when_no_snapshot(self):
        from chaos_agent.tools.kubectl_cli import _select_created_ephemeral
        pod = _pod_json(
            ["debugger-old", "debugger-last"],
            {
                "debugger-last": {"terminated": {"exitCode": 0}},
                "debugger-old": {"terminated": {"exitCode": 0}},
            },
        )
        name, attributed = _select_created_ephemeral(pod, None)
        assert name == "debugger-last"
        # snapshot fetch failed -> best guess only; caller must not treat
        # this container's terminal state as belonging to the current call
        assert attributed is False

    @pytest.mark.asyncio
    async def test_wait_keeps_polling_when_unattributed_container_terminated(
        self, monkeypatch,
    ):
        """API-lag window: the container this call created is not visible in
        spec yet, and the only candidate is a stale TERMINATED one picked by
        the unconfirmed fallback. The wait must NOT return that stale exit as
        the current call's result — it polls to the deadline instead."""
        from chaos_agent.tools.kubectl_cli import _wait_for_ephemeral_container
        pod = _pod_json(
            ["debugger-stale"],
            {"debugger-stale": {"terminated": {"exitCode": 0, "reason": "Completed"}}},
        )

        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(0, pod, "", 0.1)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        monkeypatch.setattr(kubectl_mod.settings, "timeout_kubectl_exec", 1)
        state, name, _, detail = await _wait_for_ephemeral_container(
            "tgt", "default", "",
            # snapshot fetch failed (None): the stale terminated entry must
            # not be credited to the current call
            pre_existing=None,
        )
        assert state == ""
        assert detail == "created container not visible in spec yet"

    @pytest.mark.asyncio
    async def test_wait_returns_terminated_exit0_as_success_signal(self, monkeypatch):
        from chaos_agent.tools.kubectl_cli import _wait_for_ephemeral_container
        pod = _pod_json(
            ["debugger-probe"],
            {"debugger-probe": {"terminated": {"exitCode": 0, "reason": "Completed"}}},
        )

        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(0, pod, "", 0.1)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        state, name, meta, detail = await _wait_for_ephemeral_container(
            "tgt", "default", "", pre_existing=set(),
        )
        assert state == "terminated"
        assert name == "debugger-probe"
        assert detail == "exit 0 (Completed)"
        assert meta["node"] == "node-a"

    @pytest.mark.asyncio
    async def test_wait_running_attributed_over_stale_terminated(self, monkeypatch):
        from chaos_agent.tools.kubectl_cli import _wait_for_ephemeral_container
        pod = _pod_json(
            ["debugger-zzz-stale", "debugger-fresh"],
            {
                "debugger-fresh": {"running": {}},
                "debugger-zzz-stale": {"terminated": {"exitCode": 0, "reason": "Completed"}},
            },
        )

        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(0, pod, "", 0.1)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        state, name, _, detail = await _wait_for_ephemeral_container(
            "tgt", "default", "", pre_existing={"debugger-zzz-stale"},
        )
        assert state == "running"
        assert name == "debugger-fresh"
        assert detail == ""

    @pytest.mark.asyncio
    async def test_wait_time_attribution_rescues_snapshot_failure(
        self, monkeypatch,
    ):
        """Production case (duplicate-drill debugger-zvgr4): the pre-dispatch
        snapshot fetch failed (None) and the one-shot probe container ran to
        completion within seconds. Timestamp attribution — startedAt at/after
        dispatch — must credit it, instead of polling 60s and returning a
        false 'did not start' alarm."""
        from chaos_agent.tools.kubectl_cli import _wait_for_ephemeral_container
        dispatch_ts = time.time()
        started = datetime.fromtimestamp(
            dispatch_ts + 2, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        pod = _pod_json(
            ["debugger-stale", "debugger-zvgr4"],
            {
                "debugger-stale": {"terminated": {"exitCode": 0}},
                "debugger-zvgr4": {
                    "terminated": {"exitCode": 0, "reason": "Completed", "startedAt": started},
                },
            },
        )

        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(0, pod, "", 0.1)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        state, name, _, detail = await _wait_for_ephemeral_container(
            "tgt", "default", "",
            pre_existing=None, dispatch_ts=dispatch_ts,
        )
        assert state == "terminated"
        assert name == "debugger-zvgr4"
        assert detail == "exit 0 (Completed)"

    @pytest.mark.asyncio
    async def test_wait_time_attribution_running_branch(
        self, monkeypatch,
    ):
        """Symmetric positive for the running branch: snapshot failed, the
        candidate is running with startedAt after dispatch -> ours, return
        running (the exec handle)."""
        from chaos_agent.tools.kubectl_cli import _wait_for_ephemeral_container
        dispatch_ts = time.time()
        started = datetime.fromtimestamp(
            dispatch_ts + 1, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        pod = _pod_json(
            ["debugger-stale", "debugger-ours"],
            {
                "debugger-stale": {"terminated": {"exitCode": 0}},
                "debugger-ours": {"running": {"startedAt": started}},
            },
        )

        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(0, pod, "", 0.1)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        state, name, _, detail = await _wait_for_ephemeral_container(
            "tgt", "default", "",
            pre_existing=None, dispatch_ts=dispatch_ts,
        )
        assert state == "running"
        assert name == "debugger-ours"
        assert detail == ""

    @pytest.mark.asyncio
    async def test_wait_stale_timestamp_before_dispatch_not_credited(
        self, monkeypatch,
    ):
        """Snapshot failed AND the only candidate's startedAt is an hour
        before dispatch: it belongs to an earlier drill — keep polling."""
        from chaos_agent.tools.kubectl_cli import _wait_for_ephemeral_container
        dispatch_ts = time.time()
        started = datetime.fromtimestamp(
            dispatch_ts - 3600, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        pod = _pod_json(
            ["debugger-stale"],
            {
                "debugger-stale": {
                    "terminated": {"exitCode": 0, "reason": "Completed", "startedAt": started},
                },
            },
        )

        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(0, pod, "", 0.1)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        monkeypatch.setattr(kubectl_mod.settings, "timeout_kubectl_exec", 1)
        state, _, _, detail = await _wait_for_ephemeral_container(
            "tgt", "default", "",
            pre_existing=None, dispatch_ts=dispatch_ts,
        )
        assert state == ""
        assert detail == "created container not visible in spec yet"

    @pytest.mark.asyncio
    async def test_wait_running_candidate_before_dispatch_not_credited(
        self, monkeypatch,
    ):
        """Same guard for the running branch: an unconfirmed candidate running
        since BEFORE dispatch is stale — exec must not be routed to it."""
        from chaos_agent.tools.kubectl_cli import _wait_for_ephemeral_container
        dispatch_ts = time.time()
        started = datetime.fromtimestamp(
            dispatch_ts - 3600, tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        pod = _pod_json(
            ["debugger-stale"],
            {"debugger-stale": {"running": {"startedAt": started}}},
        )

        async def fake_run(cmd, *args, **kwargs):
            return CommandResult(0, pod, "", 0.1)

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)
        monkeypatch.setattr(kubectl_mod.settings, "timeout_kubectl_exec", 1)
        state, _, _, detail = await _wait_for_ephemeral_container(
            "tgt", "default", "",
            pre_existing=None, dispatch_ts=dispatch_ts,
        )
        assert state == ""
        assert detail == "created container not visible in spec yet"


class TestOutputSafetyValve:
    """truncation-governance-consistency: error/success outputs return
    near-complete at the tool layer; governance is the compactor's job.
    The only tool-layer cut is the shared 64KB safety valve (head-tail
    middle cut, shared notice)."""

    @staticmethod
    def _invoke_with_result(monkeypatch, command_result, v_args="my-pod -n default -- ls"):
        async def fake_run(cmd, *args, **kwargs):
            return command_result

        kubectl_mod = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(kubectl_mod, "execute_via_transport", fake_run)

        async def _call():
            return await kubectl.ainvoke({
                "subcommand": "exec",
                "v_args": v_args,
                "kubeconfig": "",
            })

        return _call()

    async def test_long_error_near_complete_no_tool_layer_cut(self, monkeypatch):
        """A 5_000-char error (far over the OLD 1500 head cut) returns in
        full: no truncated marker, Error: prefix intact, TAIL verdict in."""
        blob_head = "Warning: Immediate deletion does not include finalizers\n" * 80
        verdict = "Error from server (NotFound): pods \"my-pod\" not found"
        stderr = blob_head + verdict
        assert len(stderr) > 1500  # would have been cut under the old regime

        result = await self._invoke_with_result(
            monkeypatch, CommandResult(1, "", stderr, 0.1),
        )
        assert result.startswith("Error: kubectl exec (exit 1):")
        assert verdict in result                      # tail verdict survives
        assert blob_head in result                    # head echo survives too
        assert "(truncated)" not in result            # no private dialect marker
        assert "bytes elided" not in result           # valve did not fire

    async def test_runaway_error_hits_safety_valve_with_notice(self, monkeypatch):
        """A >64KB runaway error is middle-cut: both ends kept, elision
        quantified, shared three-field notice appended."""
        blob = "W" * (70 * 1024)
        verdict = "Error from server (NotFound): pods \"my-pod\" not found"
        stderr = blob + verdict

        result = await self._invoke_with_result(
            monkeypatch, CommandResult(1, "", stderr, 0.1),
        )
        assert result.startswith("Error: kubectl exec (exit 1):")  # head intact
        assert verdict in result                     # tail verdict intact
        assert "bytes elided" in result               # quantified middle cut
        assert "⚠️ OUTPUT_TRUNCATED" in result        # shared marker family
        # honest original size: the FULL returned text (prefix + detail)
        original = f"Error: kubectl exec (exit 1): {stderr}"
        assert str(len(original.encode("utf-8"))) in result
        assert "TAIL" in result                       # kind=error guidance
        # valve ceiling is honest — notice counts against the budget
        # (same semantics as the compactor's truncate_budget)
        assert len(result.encode("utf-8")) <= 64 * 1024

    async def test_runaway_success_output_hits_valve(self, monkeypatch):
        """Same ceiling on the SUCCESS side (runaway shape exists on both
        sides); kind=success-output carries the narrowing guidance."""
        blob = "x" * (70 * 1024)

        result = await self._invoke_with_result(
            monkeypatch, CommandResult(0, blob, "", 0.1),
        )
        assert "⚠️ OUTPUT_TRUNCATED" in result
        assert "bytes elided" in result
        assert "--field-selector" in result           # success re-query guidance
        assert "TAIL" not in result                   # not the error variant

    async def test_normal_success_output_passthrough(self, monkeypatch):
        """Within budget: byte-identical passthrough, no markers, no noise."""
        stdout = "NAME  READY\nmy-pod  1/1\n"
        result = await self._invoke_with_result(
            monkeypatch, CommandResult(0, stdout, "", 0.1),
        )
        assert result == stdout


class TestQueryKubectl:
    """Tri-state read contract (B81 root fix): error ≠ empty ≠ value.

    The guard's claim discovery ran on a bare-str contract where every
    failure returned "" — a malformed command read as "found nothing".
    ``query_kubectl`` is the single production point for guard reads and
    must keep the three states distinguishable.
    """

    @pytest.mark.asyncio
    async def test_success_carries_payload(self, monkeypatch):
        from chaos_agent.tools.kubectl_cli import query_kubectl
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            return CommandResult(exit_code=0, stdout="a b c\n", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        out = await query_kubectl(["pods", "-n", "ns"], log_name="t")
        assert out.ok is True
        assert out.text == "a b c"
        assert out.words == ("a", "b", "c")
        assert out.error == ""

    @pytest.mark.asyncio
    async def test_exit_failure_is_not_ok_with_diagnosis(self, monkeypatch):
        from chaos_agent.tools.kubectl_cli import query_kubectl
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            return CommandResult(
                exit_code=1, stdout="", stderr="unknown output format",
            )

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        out = await query_kubectl(["pods", "-o", "{bad}"], log_name="t")
        assert out.ok is False
        assert out.words == ()  # fail-closed attribute
        assert "exit=1" in out.error
        assert "unknown output format" in out.error

    @pytest.mark.asyncio
    async def test_exception_is_not_ok(self, monkeypatch):
        from chaos_agent.tools.kubectl_cli import query_kubectl

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            raise RuntimeError("transport down")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        out = await query_kubectl(["pods"], log_name="t")
        assert out.ok is False
        assert out.text == ""
        assert "transport down" in out.error

    @pytest.mark.asyncio
    async def test_empty_success_is_ok_and_empty(self, monkeypatch):
        from chaos_agent.tools.kubectl_cli import query_kubectl
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            return CommandResult(exit_code=0, stdout="", stderr="")

        monkeypatch.setattr(
            "chaos_agent.transports.execute_via_transport", fake_execute,
        )
        out = await query_kubectl(["pods"], log_name="t")
        assert out.ok is True
        assert out.words == ()  # genuinely empty — the third state
        assert out.error == ""
