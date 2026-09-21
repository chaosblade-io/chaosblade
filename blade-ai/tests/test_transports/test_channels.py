"""Tests for the four TransportChannel implementations and protocol parser."""
from unittest.mock import patch

import pytest


from chaos_agent.tools.guard import CommandResult
from chaos_agent.transports.base import TransportTarget
from chaos_agent.transports.channels import (
    KubeconfigChannel,
    KubewizHostChannel,
    KubewizK8sChannel,
    SSHChannel,
    embed_stdin_in_command,
)
from chaos_agent.transports.protocol import parse_wiz_output


# ── parse_wiz_output ──────────────────────────────────────────


class TestParseWizOutput:
    def test_success(self):
        r = CommandResult(exit_code=0, stdout="exit_code: 0\npod/nginx Running\n", stderr="")
        out = parse_wiz_output(r)
        assert out.exit_code == 0
        assert out.stdout == "pod/nginx Running\n"

    def test_inner_failure(self):
        r = CommandResult(exit_code=0, stdout="exit_code: 1\nError: not found", stderr="")
        out = parse_wiz_output(r)
        assert out.exit_code == 1
        assert out.stdout == "Error: not found"

    def test_empty_output(self):
        r = CommandResult(exit_code=0, stdout="exit_code: 0\n", stderr="")
        out = parse_wiz_output(r)
        assert out.exit_code == 0
        assert out.stdout == ""

    def test_only_exit_code_line(self):
        r = CommandResult(exit_code=0, stdout="exit_code: 0", stderr="")
        out = parse_wiz_output(r)
        assert out.exit_code == 0
        assert out.stdout == ""

    def test_wiz_self_failure(self):
        r = CommandResult(exit_code=1, stdout="", stderr="timeout")
        out = parse_wiz_output(r)
        assert out.exit_code == 1
        assert out.stderr == "timeout"

    def test_protocol_violation(self):
        r = CommandResult(exit_code=0, stdout="garbage", stderr="")
        out = parse_wiz_output(r)
        assert out.exit_code == 1
        assert "wiz protocol error" in out.stderr


# ── KubeconfigChannel ─────────────────────────────────────────


class TestKubeconfigChannel:
    def setup_method(self):
        self.ch = KubeconfigChannel()

    def test_name(self):
        assert self.ch.name == "kubeconfig"

    def test_wrap_command_passthrough(self):
        cmd = ["kubectl", "get", "pods"]
        assert self.ch.wrap_command(cmd, TransportTarget()) == cmd

    def test_adapt_result_passthrough(self):
        r = CommandResult(exit_code=0, stdout="ok", stderr="")
        assert self.ch.adapt_result(r, TransportTarget()) == r

    @patch("chaos_agent.transports.channels.settings")
    def test_preflight_no_kubeconfig(self, mock_settings):
        mock_settings.kubeconfig_path = ""
        with patch("os.path.isfile", return_value=False):
            errors = self.ch.preflight(TransportTarget())
        assert len(errors) == 1
        assert "kubeconfig" in errors[0].lower()

    @patch("chaos_agent.transports.channels.settings")
    def test_preflight_valid_kubeconfig(self, mock_settings):
        mock_settings.kubeconfig_path = "/tmp/valid"
        with patch("os.path.isfile", return_value=True):
            errors = self.ch.preflight(TransportTarget())
        assert errors == []

    @patch("chaos_agent.transports.channels.settings")
    def test_preflight_multipath_kubeconfig_any_exists(self, mock_settings):
        # KUBECONFIG may be an os.pathsep-joined list; kubectl merges them.
        # Preflight must accept when at least one component exists.
        import os
        mock_settings.kubeconfig_path = f"/tmp/missing{os.pathsep}/tmp/present"
        with patch("os.path.isfile", side_effect=lambda p: p == "/tmp/present"):
            errors = self.ch.preflight(TransportTarget())
        assert errors == []

    @patch("chaos_agent.transports.channels.settings")
    def test_preflight_multipath_kubeconfig_all_missing(self, mock_settings):
        import os
        mock_settings.kubeconfig_path = f"/tmp/a{os.pathsep}/tmp/b"
        with patch("os.path.isfile", return_value=False):
            errors = self.ch.preflight(TransportTarget())
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_display_command(self):
        """The semantic command stays the subject; the LOCATION is appended.

        Stripping the wrapper to a bare command is what hid a cross-machine
        read in task-46317228, so the destination must remain visible.
        """
        shown = self.ch.display_command(["kubectl", "get", "pods"])
        assert shown.startswith("kubectl get pods")
        assert "kubeconfig" in shown


# ── KubewizK8sChannel ─────────────────────────────────────────


class TestKubewizK8sChannel:
    def setup_method(self):
        self.ch = KubewizK8sChannel()

    def test_name(self):
        assert self.ch.name == "kubewiz_k8s"

    @patch("chaos_agent.transports.channels.settings")
    def test_wrap_command(self, mock_settings):
        mock_settings.wiz_path = "/usr/local/bin/wiz"
        target = TransportTarget(kubewiz_cluster_uuid="uuid-1", kubewiz_profile="prof-1")
        wrapped = self.ch.wrap_command(["kubectl", "get", "pods"], target)
        assert wrapped[0] == "/usr/local/bin/wiz"
        assert wrapped[1:3] == ["task", "exec"]
        assert "--command" in wrapped
        assert "kubectl" in wrapped[wrapped.index("--command") + 1]
        assert "--cluster-uuid" in wrapped
        assert "uuid-1" in wrapped
        assert "--profile" in wrapped
        assert "prof-1" in wrapped

    @patch("chaos_agent.transports.channels.settings")
    def test_wrap_command_quotes_special_chars(self, mock_settings):
        mock_settings.wiz_path = "wiz"
        target = TransportTarget(kubewiz_cluster_uuid="u", kubewiz_profile="p")
        wrapped = self.ch.wrap_command(
            ["kubectl", "patch", "deploy/x", "-p", '{"spec":{"replicas":1}}'],
            target,
        )
        cmd_str = wrapped[wrapped.index("--command") + 1]
        # JSON arg must be single-quoted
        assert "'{" in cmd_str or "'" in cmd_str

    @patch("chaos_agent.transports.channels.settings")
    def test_wait_timeout_tracks_per_command_timeout(self, mock_settings):
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_wait_timeout = 0  # 0 = track per-command timeout
        mock_settings.kubewiz_task_timeout = 600
        target = TransportTarget(kubewiz_cluster_uuid="u", kubewiz_profile="p")
        wrapped = self.ch.wrap_command(["kubectl", "get", "pods"], target, timeout=120)
        assert "--wait-timeout" in wrapped
        assert wrapped[wrapped.index("--wait-timeout") + 1] == "120"
        # --timeout is INDEPENDENT of --wait-timeout (kubewiz_task_timeout).
        assert "--timeout" in wrapped
        assert wrapped[wrapped.index("--timeout") + 1] == "600"

    @patch("chaos_agent.transports.channels.settings")
    def test_wait_timeout_defaults_to_10_when_unset(self, mock_settings):
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_wait_timeout = 0
        mock_settings.kubewiz_task_timeout = 600
        target = TransportTarget(kubewiz_cluster_uuid="u", kubewiz_profile="p")
        # No per-command timeout supplied → fall back to wiz's own 10s default.
        wrapped = self.ch.wrap_command(["kubectl", "get", "pods"], target)
        assert wrapped[wrapped.index("--wait-timeout") + 1] == "10"
        # --timeout stays on its own config, unaffected by the wait fallback.
        assert wrapped[wrapped.index("--timeout") + 1] == "600"

    @patch("chaos_agent.transports.channels.settings")
    def test_wait_timeout_override_pins_value(self, mock_settings):
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_wait_timeout = 300  # explicit override
        mock_settings.kubewiz_task_timeout = 600
        target = TransportTarget(kubewiz_cluster_uuid="u", kubewiz_profile="p")
        # Override wins regardless of the per-command timeout.
        wrapped = self.ch.wrap_command(["kubectl", "get", "pods"], target, timeout=15)
        assert wrapped[wrapped.index("--wait-timeout") + 1] == "300"
        # --timeout is unaffected by the wait-timeout override.
        assert wrapped[wrapped.index("--timeout") + 1] == "600"

    @patch("chaos_agent.transports.channels.settings")
    def test_task_timeout_is_independent_config(self, mock_settings):
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_wait_timeout = 0
        mock_settings.kubewiz_task_timeout = 900  # dedicated --timeout config
        target = TransportTarget(kubewiz_cluster_uuid="u", kubewiz_profile="p")
        wrapped = self.ch.wrap_command(["kubectl", "get", "pods"], target, timeout=120)
        # --timeout follows its own config; --wait-timeout tracks the command.
        assert wrapped[wrapped.index("--timeout") + 1] == "900"
        assert wrapped[wrapped.index("--wait-timeout") + 1] == "120"

    def test_wait_timeout_shipped_default_is_mirror(self):
        # The SHIPPED default must keep the mirror branch live. A former
        # default of 30 deterministically mis-killed commands that really ran
        # >30s inside a larger caller budget (timeout_kubectl=60 /
        # timeout_kubectl_exec=180): wiz answered "task timed out after 30s"
        # while the task kept running server-side, and classify_error reads
        # that text as SHORT_RETRY — a side-effecting retry risked double
        # execution (measured live 2026-09-20). model_fields pins the FIELD
        # default so a local config.json or env var cannot mask a regression.
        from chaos_agent.config.settings import Settings
        assert Settings.model_fields["kubewiz_wait_timeout"].default == 0

    def test_adapt_result_parses_wiz(self):
        r = CommandResult(exit_code=0, stdout="exit_code: 0\nok", stderr="")
        out = self.ch.adapt_result(r, TransportTarget())
        assert out.exit_code == 0
        assert out.stdout == "ok"

    def test_preflight_missing_uuid(self):
        target = TransportTarget(kubewiz_profile="p")
        errors = self.ch.preflight(target)
        assert any("cluster_uuid" in e for e in errors)

    def test_preflight_missing_profile(self):
        target = TransportTarget(kubewiz_cluster_uuid="u")
        errors = self.ch.preflight(target)
        assert any("profile" in e for e in errors)

    def test_preflight_ok(self):
        target = TransportTarget(kubewiz_cluster_uuid="u", kubewiz_profile="p")
        assert self.ch.preflight(target) == []

    def test_display_command_strips_wrapper_but_keeps_location(self):
        wrapped = ["wiz", "task", "exec", "--command", "kubectl get pods",
                    "--cluster-uuid", "u", "--profile", "p"]
        shown = self.ch.display_command(wrapped)
        assert shown.startswith("kubectl get pods")
        # ``--cluster-uuid`` addressing means the platform executor answers, not
        # a machine the operator picked — that must not be invisible.
        assert "kubewiz_k8s" in shown
        assert "cluster u" in shown

    @patch("chaos_agent.transports.channels.settings")
    def test_wrap_command_embeds_stdin(self, mock_settings):
        """wiz has no stdin pipe — the payload must ride INSIDE --command.

        task-349ccf5d: manifest apply was deadlocked on this channel because
        stdin could never reach the executor; the occupier-pod drill (and
        anything else needing ``apply -f -``) needs this embedding.
        """
        mock_settings.wiz_path = "wiz"
        target = TransportTarget(kubewiz_cluster_uuid="u", kubewiz_profile="p")
        manifest = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: probe\n"
        wrapped = self.ch.wrap_command(
            ["kubectl", "apply", "-f", "-"], target, stdin_data=manifest,
        )
        cmd_str = wrapped[wrapped.index("--command") + 1]
        assert "base64 -d" in cmd_str
        assert "kubectl apply -f -" in cmd_str
        # Raw manifest text must NOT leak into the command line verbatim.
        assert "kind: ConfigMap" not in cmd_str

    def test_supports_stdin_is_false(self):
        assert self.ch.supports_stdin is False


# ── embed_stdin_in_command ──────────────────────────────


class TestEmbedStdinInCommand:
    def test_roundtrip(self):
        """The embedded blob must decode back to the exact payload."""
        import base64
        import re
        payload = "apiVersion: v1\nkind: Pod\nmetadata:\n  name: 'odd $name'\n"
        cmd = embed_stdin_in_command(["kubectl", "apply", "-f", "-"], payload)
        assert cmd[:2] == ["sh", "-c"]
        m = re.match(r"echo ([A-Za-z0-9+/=]+) \| base64 -d \| (.*)", cmd[2])
        assert m is not None
        assert base64.b64decode(m.group(1)).decode("utf-8") == payload

    def test_shell_metacharacters_cannot_escape(self):
        """A hostile manifest must not be able to inject extra commands.

        base64 output is a single word from a safe alphabet, so ``;``,
        backticks and ``$()`` in the payload can never reach the shell
        unencoded.
        """
        payload = "x\n`; rm -rf /`\n$(curl evil)\n"
        cmd = embed_stdin_in_command(["cat"], payload)
        script = cmd[2]
        assert "rm -rf" not in script
        assert "curl" not in script

    def test_inner_command_is_quoted(self):
        cmd = embed_stdin_in_command(
            ["kubectl", "patch", "deploy/x", "-p", '{"spec":{"replicas":1}}'],
            "data",
        )
        # The JSON arg keeps its quoting inside the sh -c script.
        assert "'{\"spec\":{\"replicas\":1}}'" in cmd[2]


# ── KubewizHostChannel ────────────────────────────────────────


class TestKubewizHostChannel:
    def setup_method(self):
        self.ch = KubewizHostChannel()

    def test_name(self):
        assert self.ch.name == "kubewiz_host"

    @patch("chaos_agent.transports.channels.settings")
    def test_wrap_command(self, mock_settings):
        mock_settings.wiz_path = "wiz"
        target = TransportTarget(host_name="10.0.0.1", kubewiz_profile="prof")
        wrapped = self.ch.wrap_command(["iptables", "-L"], target)
        assert "--cluster-uuid" in wrapped
        idx = wrapped.index("--cluster-uuid")
        assert wrapped[idx + 1] == "kubewiz-host-channel"
        assert "--name" in wrapped
        assert "10.0.0.1" in wrapped

    def test_adapt_result_parses_wiz(self):
        r = CommandResult(exit_code=0, stdout="exit_code: 0\nchain OUTPUT", stderr="")
        out = self.ch.adapt_result(r, TransportTarget())
        assert out.exit_code == 0
        assert out.stdout == "chain OUTPUT"

    @patch("chaos_agent.transports.channels.settings")
    def test_floor_value_mirrors_verbatim(self, mock_settings):
        """R62: the R61 floor (1s) is the value this face MIRRORS. Before
        R61 an LLM-supplied 0 reached the fallback branch (10s); now the
        floored 1 arrives here and is mirrored verbatim — pin the mirror
        semantics of the floor so this consumer chain stays observable."""
        mock_settings.wiz_path = "wiz"
        mock_settings.kubewiz_wait_timeout = 0  # mirror mode
        mock_settings.kubewiz_task_timeout = 600
        target = TransportTarget(host_name="10.0.0.1", kubewiz_profile="prof")
        wrapped = self.ch.wrap_command(["uptime"], target, timeout=1)
        assert wrapped[wrapped.index("--wait-timeout") + 1] == "1"
        # The server-side task budget is independent and stays 600.
        assert wrapped[wrapped.index("--timeout") + 1] == "600"

    def test_preflight_missing_host_name(self):
        target = TransportTarget(kubewiz_profile="p")
        errors = self.ch.preflight(target)
        assert any("host_name" in e for e in errors)

    def test_preflight_ok(self):
        target = TransportTarget(host_name="10.0.0.1", kubewiz_profile="p")
        assert self.ch.preflight(target) == []

    @patch("chaos_agent.transports.channels.settings")
    def test_wrap_command_embeds_stdin(self, mock_settings):
        """Same wiz limitation as the k8s channel — no native stdin pipe."""
        mock_settings.wiz_path = "wiz"
        target = TransportTarget(host_name="10.0.0.1", kubewiz_profile="prof")
        wrapped = self.ch.wrap_command(["cat"], target, stdin_data="payload\n")
        cmd_str = wrapped[wrapped.index("--command") + 1]
        assert "base64 -d" in cmd_str
        assert "payload" not in cmd_str

    def test_supports_stdin_is_false(self):
        assert self.ch.supports_stdin is False


class TestStdinCapabilityMatrix:
    """The executor routes stdin by this flag — it must never drift silently.

    Native channels (kubeconfig/ssh) pipe stdin to the subprocess; wiz
    channels fold it into the command. Flipping either side without the
    other breaks manifest apply on that channel (task-349ccf5d).
    """

    def test_matrix(self):
        assert KubeconfigChannel.supports_stdin is True
        assert SSHChannel.supports_stdin is True
        assert KubewizK8sChannel.supports_stdin is False
        assert KubewizHostChannel.supports_stdin is False


# ── SSHChannel ────────────────────────────────────────────────


class TestSSHChannel:
    def setup_method(self):
        self.ch = SSHChannel()

    def test_name(self):
        assert self.ch.name == "ssh"

    def test_wrap_command_basic(self):
        target = TransportTarget(ssh_host="10.0.0.1", ssh_user="root")
        wrapped = self.ch.wrap_command(["iptables", "-L"], target)
        assert wrapped[0] == "ssh"
        assert "root@10.0.0.1" in wrapped
        assert "iptables" in wrapped[-1]

    def test_wrap_command_with_key(self):
        target = TransportTarget(
            ssh_host="10.0.0.1", ssh_user="root", ssh_key_path="/tmp/key", ssh_port=2222
        )
        wrapped = self.ch.wrap_command(["df", "-h"], target)
        assert "-i" in wrapped
        assert "/tmp/key" in wrapped
        assert "-p" in wrapped
        assert "2222" in wrapped

    def test_adapt_result_passthrough(self):
        r = CommandResult(exit_code=0, stdout="ok", stderr="")
        assert self.ch.adapt_result(r, TransportTarget()) == r

    @patch("chaos_agent.transports.channels.settings")
    def test_wrap_command_default_strict_host_key_checking(self, mock_settings):
        """Default StrictHostKeyChecking policy (accept-new) must be emitted."""
        mock_settings.ssh_strict_host_key_checking = "accept-new"
        target = TransportTarget(ssh_host="10.0.0.1", ssh_user="root")
        wrapped = self.ch.wrap_command(["iptables", "-L"], target)
        assert "StrictHostKeyChecking=accept-new" in wrapped
        assert "BatchMode=yes" in wrapped

    @patch("chaos_agent.transports.channels.settings")
    def test_wrap_command_uses_configured_strict_host_key_checking(self, mock_settings):
        """A configured policy (e.g. 'yes') must be passed through verbatim."""
        mock_settings.ssh_strict_host_key_checking = "yes"
        target = TransportTarget(ssh_host="10.0.0.1")
        wrapped = self.ch.wrap_command(["df", "-h"], target)
        assert "StrictHostKeyChecking=yes" in wrapped

    @patch("chaos_agent.transports.channels.settings")
    def test_wrap_command_terminates_options_with_double_dash(self, mock_settings):
        """'--' must precede user_host so an ssh_host beginning with '-'
        cannot be misparsed as an ssh flag (option-injection hardening)."""
        mock_settings.ssh_strict_host_key_checking = "accept-new"
        target = TransportTarget(ssh_host="-oProxyCommand=evil", ssh_user="root")
        wrapped = self.ch.wrap_command(["id"], target)
        dd = wrapped.index("--")
        # user_host and cmd_str come immediately after the terminator.
        assert wrapped[dd + 1] == "root@-oProxyCommand=evil"
        assert "id" in wrapped[dd + 2]

    def test_preflight_missing_host(self):
        errors = self.ch.preflight(TransportTarget())
        assert any("ssh_host" in e for e in errors)

    def test_preflight_ok(self):
        target = TransportTarget(ssh_host="10.0.0.1")
        with patch("os.path.isfile", return_value=True):
            errors = self.ch.preflight(target)
        assert errors == []


class TestTransportAnomalyExplanation:
    """A JSON-parse complaint about the REPLY must not read as command failure.

    task-46317228 #64/#66: the gateway answered with an HTML page. ``wiz``
    (Node) reported ``Unexpected token '<', "<!DOCTYPE "... is not valid JSON``
    and ``blade`` (Go) reported ``invalid character 'b' after top-level
    value``. Both were relayed verbatim; the LLM read them as "injection
    failed", invented a ``kubectl debug --image=stress-ng`` second injection and
    left a stray debug pod — while blade_status already said Running/Success.
    """

    @pytest.mark.parametrize("raw", [
        "Error: Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON",
        "Error: invalid character 'b' after top-level value",
        "<html><head><title>502 Bad Gateway</title></head></html>",
    ])
    def test_recognises_non_json_replies(self, raw):
        from chaos_agent.transports.protocol import explain_transport_anomaly

        explanation = explain_transport_anomaly(raw)
        assert explanation, f"should recognise: {raw}"
        # It must remove the false premise — that the parse error is a verdict on
        # the command — by stating WHERE the failure happened.
        assert "PARSING" in explanation
        assert "outcome is not reported here" in explanation

    @pytest.mark.parametrize("raw", [
        "Error: Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON",
        "<html><head><title>502 Bad Gateway</title></head></html>",
    ])
    def test_explanation_states_a_fact_and_nothing_more(self, raw):
        """This layer reports facts; guessing and instructing belong elsewhere.

        The codebase's rule is "surface the raw signal without a verdict"
        (``kubectl._kubectl_impl``). The first version of this text guessed the
        cause ("likely an auth failure, or a timeout") and prescribed behaviour
        ("do NOT retry with a different injection method"). Both overstepped: the
        speculation gets quoted in the model's verdict and can collide with
        ``classify_error``'s patterns, and the instruction already exists where it
        belongs — in ``blade_create``'s own failure message and the phase prompts.
        """
        from chaos_agent.transports.protocol import explain_transport_anomaly

        explanation = explain_transport_anomaly(raw)
        for guess in ("likely", "probably", "auth failure", "timeout"):
            assert guess not in explanation.lower(), (
                f"the annotation speculates about the cause ({guess!r})"
            )
        for directive in ("do not", "don't", "you must", "re-check"):
            assert directive not in explanation.lower(), (
                f"the annotation prescribes behaviour ({directive!r}) — that is "
                f"policy and belongs in the prompt, not in a tool result"
            )

    @pytest.mark.parametrize("raw", [
        'Error from server (NotFound): pods "x" not found',
        "exit status 1: cpu-percent must be 1-100",
        "command terminated with exit code 137",
        # A bare "invalid character" is an ARGUMENT complaint from
        # kubectl/blade/iptables, not a JSON decoder. Matching it would label a
        # genuine failure as "may not mean the command failed" — the dangerous
        # direction. Only Go's position clauses identify a real decode error.
        'error: invalid character in resource name "a b"',
        "blade: invalid character in flag value",
        "iptables v1.8.7: invalid character in chain name",
        "",
    ])
    def test_leaves_genuine_failures_alone(self, raw):
        """A real command failure must not be softened into a transport note."""
        from chaos_agent.transports.protocol import explain_transport_anomaly

        assert explain_transport_anomaly(raw) == ""

    @pytest.mark.parametrize("raw", [
        "invalid character 'b' after top-level value",
        "invalid character '<' looking for beginning of value",
        "invalid character '}' looking for beginning of object key string",
    ])
    def test_recognises_go_json_decoder_errors(self, raw):
        """Go's encoding/json always names the position it choked at."""
        from chaos_agent.transports.protocol import explain_transport_anomaly

        assert explain_transport_anomaly(raw)

    def test_parse_wiz_output_annotates_stderr(self):
        from chaos_agent.models.command_result import CommandResult
        from chaos_agent.transports.protocol import parse_wiz_output

        result = parse_wiz_output(CommandResult(
            exit_code=1, stdout="",
            stderr="Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON",
        ))
        assert "transport error" in result.stderr
        # The raw reply must still be present for diagnosis.
        assert "<!DOCTYPE" in result.stderr

    def test_parse_wiz_output_preserves_plain_failures(self):
        from chaos_agent.models.command_result import CommandResult
        from chaos_agent.transports.protocol import parse_wiz_output

        original = CommandResult(exit_code=1, stdout="", stderr="wiz: connection refused")
        assert parse_wiz_output(original) is original


class TestAnomalyAnnotationDoesNotChangeControlFlow:
    """The explanation is for the MODEL, never for the code that classifies.

    Two independent guards, because this already went wrong once: the first
    version of the text mentioned "timeout" as a possible cause, which matches
    ``classify_error``'s INFRA_TRANSIENT rule and turned a terminal END_FAILED
    into a SHORT_RETRY that polls the cluster for nothing.
    """

    def test_the_explanation_is_classifier_neutral(self):
        """Guard 1 — the text must not match any classification rule.

        Keeping it factual ("the reply was not JSON") rather than speculative
        ("probably an auth failure or a timeout") removes the collision at the
        source instead of relying on call sites keeping the strings apart.
        """
        from chaos_agent.errors import ErrorClass, classify_error
        from chaos_agent.transports.protocol import _NON_JSON_EXPLANATION

        verdict = classify_error(_NON_JSON_EXPLANATION)
        assert verdict.error_class is ErrorClass.UNKNOWN, (
            f"the annotation text matches rule {verdict.matched_pattern!r}; a "
            f"word in prose must never decide a failure's handling"
        )

    def test_annotating_cannot_change_the_verdict(self):
        """Guard 2 — even prefixed, the classification must be unchanged."""
        from chaos_agent.errors import ErrorAction, classify_error
        from chaos_agent.transports.protocol import _NON_JSON_EXPLANATION

        raw = "invalid character 'b' after top-level value"
        assert classify_error(raw).action == ErrorAction.END_FAILED
        assert (
            classify_error(f"{_NON_JSON_EXPLANATION}\n{raw}").action
            == classify_error(raw).action
        )

    def test_blade_create_source_keeps_the_two_strings_apart(self):
        """Guard 3 — classification still reads the RAW output, by construction."""
        import inspect

        from chaos_agent.agent.providers.chaosblade import cli as blade

        # ``blade_create`` is a StructuredTool; the body lives on its coroutine.
        src = inspect.getsource(blade.blade_create.coroutine)
        assert "_shown = " in src
        assert "classify_error(combined)" in src, (
            "classification must read the RAW combined output"
        )
        assert "combined = f\"{_anomaly}" not in src, (
            "the annotation must not be folded back into combined"
        )

    def test_the_raw_reply_is_never_replaced(self):
        """The annotation is additive: the original text must survive verbatim."""
        from chaos_agent.models.command_result import CommandResult
        from chaos_agent.transports.protocol import parse_wiz_output

        raw = "Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON"
        out = parse_wiz_output(CommandResult(exit_code=1, stdout="", stderr=raw))
        assert raw in out.stderr
        assert out.stderr.endswith(raw), "the raw reply must come last, unaltered"


# ── wiz stderr-preservation wrapper ──────────────────────────

class TestWizStderrPreservation:
    """Probed live: the wiz platform drops the remote command's stderr when
    stdout is non-empty (incident inject-774ecd39: kubectl half-rendered a
    jsonpath on stdout, the real parse error sat on stderr and never arrived,
    leaving the model self-repairing blind). The wrapper tees stderr to a
    temp file and appends it to stdout ONLY on non-zero exit."""

    def test_wrapper_shape(self):
        from chaos_agent.transports.channels import _wiz_preserve_stderr

        wrapped = _wiz_preserve_stderr(["kubectl", "get", "pods"])
        # Platform word-splits --command and execs directly (probed live);
        # shell logic only survives inside an explicit sh -c.
        assert wrapped.startswith("sh -c '")
        assert "( kubectl get pods ) 2>\"$E\"" in wrapped
        assert '[ $rc -ne 0 ] && { printf "\\n"; cat "$E"; }' in wrapped
        assert wrapped.rstrip("'").endswith("exit $rc")

    def test_wrapper_quotes_inner_args(self):
        from chaos_agent.transports.channels import _wiz_preserve_stderr

        wrapped = _wiz_preserve_stderr(
            ["kubectl", "patch", "deploy/x", "-p", '{"spec":{"replicas":1}}']
        )
        assert "'{\"spec\":{\"replicas\":1}}'" in wrapped

    def test_unwrap_roundtrip(self):
        from chaos_agent.transports.channels import (
            _wiz_preserve_stderr,
            _wiz_unwrap_for_display,
        )

        cmd = ["kubectl", "patch", "deploy/x", "-p", '{"spec":{"replicas":1}}']
        inner = _wiz_unwrap_for_display(_wiz_preserve_stderr(cmd))
        assert inner == "kubectl patch deploy/x -p '{\"spec\":{\"replicas\":1}}'"

    @pytest.mark.parametrize("inner_cmd, expect_out, rc", [
        # failure with BOTH streams → stderr appended after a newline
        # separator (a mid-line stdout must not fuse with the first stderr line)
        (["sh", "-c", "printf partial; printf real-error >&2; exit 3"],
         "partial\nreal-error", 3),
        # failure with stderr only → still surfaces (leading blank line is
        # the separator; stdout was empty)
        (["sh", "-c", "printf only-err >&2; exit 1"], "\nonly-err", 1),
        # success with stderr noise → stdout stays byte-identical
        (["sh", "-c", "printf clean-ok; printf warn >&2"], "clean-ok", 0),
    ])
    def test_wrapper_semantics_via_local_shell(self, inner_cmd, expect_out, rc):
        import subprocess

        from chaos_agent.transports.channels import _wiz_preserve_stderr

        p = subprocess.run(
            ["sh", "-c", _wiz_preserve_stderr(inner_cmd)],
            capture_output=True, text=True, timeout=10,
        )
        assert p.returncode == rc
        assert p.stdout == expect_out

    @patch("chaos_agent.transports.channels.settings")
    def test_k8s_channel_wraps_command(self, mock_settings):
        mock_settings.wiz_path = "wiz"
        target = TransportTarget(kubewiz_cluster_uuid="u", kubewiz_profile="p")
        wrapped = KubewizK8sChannel().wrap_command(["kubectl", "get", "pods"], target)
        cmd_str = wrapped[wrapped.index("--command") + 1]
        assert cmd_str.startswith("sh -c ")

    @patch("chaos_agent.transports.channels.settings")
    def test_host_channel_wraps_command(self, mock_settings):
        mock_settings.wiz_path = "wiz"
        target = TransportTarget(host_name="h1", kubewiz_profile="p")
        wrapped = KubewizHostChannel().wrap_command(["df", "-h"], target)
        cmd_str = wrapped[wrapped.index("--command") + 1]
        assert cmd_str.startswith("sh -c ")
