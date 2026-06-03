"""Tests for guard_parser — AST-level kubectl/blade command parsing."""

import pytest

from chaos_agent.tools.guard_parser import (
    BLADE_BOOLEAN_FLAGS,
    BLADE_SUBCOMMANDS,
    KUBECTL_BOOLEAN_FLAGS,
    KUBECTL_DATA_PAYLOAD_FLAGS,
    KUBECTL_DOUBLE_DASH_SUBCOMMANDS,
    SUSPICIOUS_SOLO_TOKENS,
    ParsedCommand,
    parse_command,
)


class TestKubectlBasic:
    def test_simple_get(self):
        p = parse_command(["kubectl", "get", "pods"])
        assert p.binary == "kubectl"
        assert p.subcommand == "get"
        assert p.positional_args == ("pods",)
        assert p.flags == ()

    def test_get_with_namespace_value_flag(self):
        p = parse_command(["kubectl", "get", "pods", "-n", "default"])
        assert p.subcommand == "get"
        assert p.positional_args == ("pods",)
        assert p.flags == (("-n", "default"),)

    def test_get_with_equals_syntax(self):
        p = parse_command(["kubectl", "get", "pods", "--namespace=default"])
        assert p.flags == (("--namespace", "default"),)

    def test_global_flags_before_subcommand(self):
        p = parse_command([
            "kubectl", "--kubeconfig", "/k", "--context", "c", "get", "pods",
        ])
        assert p.subcommand == "get"
        assert p.positional_args == ("pods",)
        assert ("--kubeconfig", "/k") in p.flags
        assert ("--context", "c") in p.flags


class TestKubectlBooleanFlags:
    def test_dash_A_does_not_consume_next_token(self):
        """Regression: -A is boolean, must not eat the next positional."""
        p = parse_command(["kubectl", "get", "-A", "pod"])
        assert p.subcommand == "get"
        assert p.positional_args == ("pod",)
        assert ("-A", None) in p.flags

    def test_watch_flag(self):
        p = parse_command(["kubectl", "get", "-w", "pods"])
        assert p.positional_args == ("pods",)
        assert ("-w", None) in p.flags

    def test_help_boolean(self):
        p = parse_command(["kubectl", "--help"])
        assert ("--help", None) in p.flags
        assert p.subcommand is None

    def test_multiple_booleans(self):
        p = parse_command(["kubectl", "get", "-A", "--show-labels", "pods"])
        assert p.positional_args == ("pods",)
        assert ("-A", None) in p.flags
        assert ("--show-labels", None) in p.flags


class TestKubectlUnknownFlag:
    def test_unknown_flag_defaults_to_value_taking(self):
        """Unknown flag consumes next token. Conservative: subcommand
        identification still correct; only positional may shift."""
        p = parse_command(["kubectl", "get", "--made-up-flag", "value", "pods"])
        assert p.subcommand == "get"
        assert ("--made-up-flag", "value") in p.flags
        assert p.positional_args == ("pods",)

    def test_unknown_flag_at_end_of_cmd(self):
        """Unknown flag with no following token records as value-less."""
        p = parse_command(["kubectl", "get", "pods", "--xyz"])
        assert ("--xyz", None) in p.flags


class TestKubectlDataPayloadFlags:
    def test_patch_value_in_payloads(self):
        p = parse_command([
            "kubectl", "patch", "pvc", "my-pvc",
            "-p", '{"spec":{"x":1}}',
        ])
        assert '{"spec":{"x":1}}' in p.data_payload_values
        assert ("-p", '{"spec":{"x":1}}') in p.flags

    def test_patch_equals_syntax(self):
        p = parse_command([
            "kubectl", "patch", "pvc", "x",
            '--patch={"spec":{"y":2}}',
        ])
        assert '{"spec":{"y":2}}' in p.data_payload_values

    def test_filename_payload(self):
        p = parse_command(["kubectl", "get", "-f", "manifest.yaml"])
        assert "manifest.yaml" in p.data_payload_values

    def test_field_selector_payload(self):
        """--field-selector value may contain shell-looking chars;
        must land in payloads to skip regex check."""
        p = parse_command([
            "kubectl", "get", "pods",
            "--field-selector", "status.phase=Running",
        ])
        assert "status.phase=Running" in p.data_payload_values

    def test_kubeconfig_value_is_payload(self):
        """E11 polish: --kubeconfig value is a file path, never a
        shell command. Listed as data_payload so legitimate paths
        containing shell-look-alike chars don't trip false positives."""
        p = parse_command([
            "kubectl", "--kubeconfig", "/tmp/foo;bar/cfg", "get", "pods",
        ])
        assert "/tmp/foo;bar/cfg" in p.data_payload_values

    def test_context_token_server_values_are_payload(self):
        for flag in ("--context", "--user", "--server", "--token", "--cluster"):
            p = parse_command(["kubectl", flag, "weird;value", "get", "pods"])
            assert "weird;value" in p.data_payload_values, f"flag {flag} should put value in payloads"


class TestKubectlDoubleDash:
    def test_exec_double_dash_splits_container(self):
        p = parse_command([
            "kubectl", "exec", "my-pod", "--",
            "ls", "-la", "/tmp",
        ])
        assert p.subcommand == "exec"
        assert p.positional_args == ("my-pod",)
        assert p.container_command == ("ls", "-la", "/tmp")

    def test_run_double_dash_splits_container(self):
        p = parse_command(["kubectl", "run", "x", "--", "echo", "hi"])
        assert p.subcommand == "run"
        assert p.container_command == ("echo", "hi")

    def test_get_double_dash_treated_as_positional(self):
        """Regression: `--` outside exec/run/attach/debug MUST NOT split
        container_command — it's just a token. Otherwise misplaced `--`
        would silently bypass shell checks (Gap B)."""
        p = parse_command(["kubectl", "get", "--", "pod"])
        assert p.subcommand == "get"
        assert p.container_command == ()
        # "--" and "pod" both end up in flags or positional, NOT skipped
        all_seen = list(p.positional_args) + [n for n, _ in p.flags]
        assert "pod" in all_seen


class TestKubectlNoSubcommand:
    def test_just_binary(self):
        p = parse_command(["kubectl"])
        assert p.subcommand is None
        assert p.positional_args == ()

    def test_only_global_flags(self):
        p = parse_command(["kubectl", "--kubeconfig", "/k"])
        assert p.subcommand is None
        assert ("--kubeconfig", "/k") in p.flags


class TestBlade:
    def test_create_pod_network(self):
        p = parse_command([
            "blade", "create", "pod", "network", "delay",
            "--time", "3000", "--interface", "eth0",
        ])
        assert p.binary == "blade"
        assert p.subcommand == "create"
        assert "pod" in p.positional_args
        assert "network" in p.positional_args
        assert ("--time", "3000") in p.flags
        assert ("--interface", "eth0") in p.flags

    def test_blade_subcommand_in_known_set(self):
        for sub in ("create", "destroy", "status", "prepare", "revoke"):
            p = parse_command(["blade", sub])
            assert p.subcommand == sub
            assert sub in BLADE_SUBCOMMANDS

    def test_blade_boolean_h_does_not_consume_next(self):
        """Regression: -h must not eat next token (Gap A)."""
        p = parse_command(["blade", "create", "-h", "pod"])
        assert p.subcommand == "create"
        assert "pod" in p.positional_args
        assert ("-h", None) in p.flags

    def test_blade_double_dash_not_special(self):
        """blade doesn't use `--` separator; it's just a positional."""
        p = parse_command(["blade", "destroy", "--"])
        # `--` itself starts with `-` so goes into flags as value-less
        # (parser treats it as a flag with no value). Either way, no
        # container_command split.
        assert p.container_command == ()


class TestGenericBinary:
    def test_df_all_positional(self):
        p = parse_command(["df", "-h", "/var"])
        assert p.binary == "df"
        assert p.subcommand is None
        # generic parser puts everything after binary in positional
        assert p.positional_args == ("-h", "/var")

    def test_unknown_binary_full_path(self):
        p = parse_command(["/usr/bin/sleep", "5"])
        assert p.binary == "sleep"
        assert p.positional_args == ("5",)


class TestEmpty:
    def test_empty_cmd(self):
        p = parse_command([])
        assert p.binary == ""
        assert p.host_relevant_tokens() == ("",)


class TestCombinedShortFlagContract:
    """E11 deep-self-check P2 #1 contract: combined short flag like
    `-it` is NOT correctly recognized (would require per-char parsing
    + per-flag schema). Parser falls through to value-taking and
    consumes the next token as the flag value, causing positional_args
    to lose the pod name.

    Locked behavior: pod name MUST still appear in host_relevant_tokens
    (as the flag value) so the regex scan still covers it. If parser
    is later upgraded to handle combined short flags, this test should
    flip its assertion from "in flags" to "in positional_args"."""

    def test_kubectl_exec_dash_it_pod_name_in_host_tokens(self):
        from chaos_agent.tools.guard_parser import parse_command
        p = parse_command([
            "kubectl", "exec", "-it", "pod-1", "--", "bash",
        ])
        # pod-1 is misclassified as -it value (latent limitation)
        assert ("-it", "pod-1") in p.flags or "pod-1" in p.positional_args
        # CRITICAL: regardless of where it lands, host_relevant_tokens
        # MUST include pod-1 so shell-pattern checks still scan it.
        assert "pod-1" in p.host_relevant_tokens()
        # container_command split still works
        assert p.container_command == ("bash",)


class TestHostRelevantTokens:
    def test_excludes_container_command(self):
        p = parse_command([
            "kubectl", "exec", "pod", "--", "rm", "-rf", "/",
        ])
        toks = p.host_relevant_tokens()
        assert "rm" not in toks
        assert "-rf" not in toks
        assert "pod" in toks

    def test_excludes_data_payload_values(self):
        payload = '{"spec":{"$(":"danger"}}'
        p = parse_command([
            "kubectl", "patch", "pvc", "x", "-p", payload,
        ])
        toks = p.host_relevant_tokens()
        assert payload not in toks
        # Flag name is still checked
        assert "-p" in toks

    def test_includes_binary_subcommand_positionals_flag_values(self):
        p = parse_command([
            "kubectl", "get", "pods", "-n", "default", "-o", "json",
        ])
        toks = p.host_relevant_tokens()
        assert "kubectl" in toks
        assert "get" in toks
        assert "pods" in toks
        assert "-n" in toks
        assert "default" in toks
        assert "-o" in toks
        assert "json" in toks


class TestSchemasExposed:
    """Schemas must be importable for runtime extension."""

    def test_kubectl_boolean_includes_dash_A(self):
        assert "-A" in KUBECTL_BOOLEAN_FLAGS
        assert "--all-namespaces" in KUBECTL_BOOLEAN_FLAGS

    def test_kubectl_data_payload_includes_patch(self):
        assert "-p" in KUBECTL_DATA_PAYLOAD_FLAGS
        assert "--patch" in KUBECTL_DATA_PAYLOAD_FLAGS

    def test_kubectl_double_dash_subcommands(self):
        assert "exec" in KUBECTL_DOUBLE_DASH_SUBCOMMANDS
        assert "get" not in KUBECTL_DOUBLE_DASH_SUBCOMMANDS

    def test_blade_boolean_includes_help(self):
        assert "-h" in BLADE_BOOLEAN_FLAGS
        assert "--help" in BLADE_BOOLEAN_FLAGS

    def test_suspicious_solo_tokens(self):
        for t in (";", "|", "&", "||", "&&", ">", "<", ">>"):
            assert t in SUSPICIOUS_SOLO_TOKENS
