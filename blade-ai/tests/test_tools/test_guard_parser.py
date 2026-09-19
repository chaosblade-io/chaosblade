"""Tests for guard_parser — AST-level kubectl/blade command parsing."""

import re
import shutil
import subprocess

import pytest

from chaos_agent.tools.guard_parser import (
    BLADE_BOOLEAN_FLAGS,
    BLADE_SUBCOMMANDS,
    KUBECTL_BOOLEAN_FLAGS,
    KUBECTL_DATA_PAYLOAD_FLAGS,
    KUBECTL_DOUBLE_DASH_SUBCOMMANDS,
    KUBECTL_NOOPT_DEFVAL_FLAGS,
    KUBECTL_SUBCOMMAND_BOOLEAN_SHORTHANDS,
    SUSPICIOUS_SOLO_TOKENS,
    parse_command,
)

# kubectl v1.34 help prints one flag per line, the marker (default value)
# glued with ``=``, and the description on FOLLOWING lines — so a flag line
# ends with ``:``. Two declaration shapes exist: ``-p, --previous=false:``
# (marker present) and ``-h, --help:`` (no ``=``, always boolean). The
# marker group is optional so BOTH faces are scanned — R49: the no-eq face
# was a blind spot of the original ``=(.*):$`` pattern (a future no-eq
# boolean shorthand colliding with the payload table would have slipped
# through silently, the same class of miss R48 closed for ``-p``).
_HELP_FLAG_LINE = re.compile(
    r"^\s+(?:(-[A-Za-z]), )?(--[A-Za-z0-9-]+)(?:=(.*))?:$"
)

# Every subcommand whose --help the drift tests walk. Hardcoded on purpose
# (R47): the scan surface must not silently track the guard's admit set —
# a widening there should show up as a deliberate diff here. ``options`` is
# the global-flag page (no --help suffix).
_HELP_SUBS = (
    "get", "describe", "delete", "replace", "exec", "logs", "top",
    "patch", "set", "scale", "debug", "wait", "cordon", "uncordon",
    "taint", "label", "annotate", "drain", "apply", "create",
    "rollout", "version", "cluster-info", "api-resources", "explain",
    "auth", "config", "run", "attach", "options",
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


class TestKubectlBooleanVocabulary:
    """R47: the boolean table is a SYNOPSIS vocabulary, not a "frequently
    used" sample (the R40/R41 process rule — tables are diffed against the
    tool's own help, never memory-enumerated). 42 members were missing, and
    an omitted boolean is read as value-taking: the next token is eaten —
    which, when the flag precedes the verb, is the SUBCOMMAND itself, so the
    allowlist then judges the following token."""

    # The members the R47 mechanical diff found missing (kubectl v1.34.1,
    # every subcommand ``guard.py`` admits plus ``kubectl options``).
    ADDED_IN_R47 = (
        "--follow", "--all-containers", "--all-pods", "--ignore-errors",
        "--insecure-skip-tls-verify-backend", "--privileged", "--rm",
        "--expose", "--leave-stdin-open", "--command", "--attach",
        "--arguments-only", "--replace", "--same-node", "--share-processes",
        "--keep-annotations", "--keep-init-containers", "--keep-labels",
        "--keep-liveness", "--keep-readiness", "--keep-startup",
        "--ignore-daemonsets", "--disable-eviction", "--delete-emptydir-data",
        "--now", "--wait", "--server-side", "--force-conflicts",
        "--openapi-patch", "--edit", "--windows-line-endings",
        "--save-config", "--list", "--local", "--overwrite", "--cached",
        "--namespaced", "--client", "--output-watch-events",
        "--disable-compression", "--match-server-version",
        "--warnings-as-errors",
    )

    @pytest.mark.parametrize("flag", ADDED_IN_R47)
    def test_family_boolean_is_listed(self, flag):
        assert flag in KUBECTL_BOOLEAN_FLAGS, flag

    @pytest.mark.parametrize(
        "flag",
        ["--warnings-as-errors", "--disable-compression", "--match-server-version"],
    )
    def test_global_boolean_does_not_eat_the_verb(self, flag):
        p = parse_command(["kubectl", flag, "get", "pods"])
        assert p.subcommand == "get", p.subcommand
        assert p.positional_args == ("pods",)

    def test_logs_short_f_is_follow_not_filename(self):
        # ``-f`` is one of the subcommand-dependent pair (``-p`` joined it
        # in R48): ``--follow`` (boolean) under ``logs``, ``--filename``
        # (value) elsewhere. The logs reading is resolved in the parser
        # because the flat payload table classifies ``-f`` as
        # ``--filename`` — an omitted resolution here payload-skipped the
        # following token, losing host checks.
        p = parse_command(["kubectl", "logs", "-f", "mypod", "-n", "ns"])
        assert p.positional_args == ("mypod",)
        assert "mypod" in p.host_relevant_tokens()
        assert p.data_payload_values == ()

    def test_filename_short_f_elsewhere_is_still_a_value(self):
        # The resolution must not leak: get/exec keep ``--filename``.
        p = parse_command(["kubectl", "get", "-f", "x.yaml"])
        assert p.data_payload_values == ("x.yaml",)
        p = parse_command(
            ["kubectl", "exec", "-f", "f.yaml", "mypod", "--", "cat", "/f"]
        )
        assert ("-f", "f.yaml") in p.flags

    def test_synopsis_diff_stays_empty(self):
        """The process rule, executable — at NAME level (R48). R47's version
        compared a whole declaration's name-set against the flat table and
        passed if ANY name was covered: the covered long twin ``--previous``
        masked the uncovered shorthand ``-p`` — the very member whose
        payload-table reading made it an escape. Every name of every boolean
        declaration must be covered, keyed by the subcommand that declares
        it (the override table is subcommand-scoped). Needs the real binary
        (skipped where absent); a future kubectl that adds a boolean names
        it here instead of silently swallowing a token."""
        if shutil.which("kubectl") is None:
            pytest.skip("kubectl not on PATH")

        opt = _HELP_FLAG_LINE
        missing: list[tuple[str, str]] = []
        for sub in _HELP_SUBS:
            argv = ["kubectl", sub] + ([] if sub == "options" else ["--help"])
            text = subprocess.run(
                argv, capture_output=True, text=True, check=False
            ).stdout
            covered = KUBECTL_BOOLEAN_FLAGS | set(
                KUBECTL_SUBCOMMAND_BOOLEAN_SHORTHANDS.get(sub, ())
            )
            for line in text.splitlines():
                m = opt.match(line)
                if m is None:
                    continue
                marker = m.group(3)
                # R49: ``marker is None`` is the no-``=`` boolean face
                # (``-h, --help:``) — scanned now, invisible to the old
                # ``=(.*):$`` regex. An EMPTY marker (``--vmodule=:``) is a
                # value declaration and stays skipped.
                if marker is not None and marker not in ("false", "true"):
                    continue
                for name in (m.group(2), m.group(1)):
                    if name and name not in covered:
                        missing.append((sub, name))
        assert not missing, f"uncovered boolean names: {missing}"


class TestKubectlSubcommandShorthands:
    """R48: the second member of the subcommand-dependent class. ``-p`` is
    ``--patch`` (value, payload table) under every subcommand and
    ``--previous`` (boolean) under ``logs`` — the collision lives in SHORT
    names, which is why R47's LONG-name dependency scan (and its set-level
    missing check) never saw it. Read as value-taking, ``-p`` swallowed the
    next token AND payload-skipped it: the same measured escape chain as
    R47's ``-f``. The table is the mechanical result, re-derivable by
    intersecting every admitted subcommand's boolean shorthands with the
    payload table's shorthands (``logs`` collides; ``-f``/``-p`` are its
    only members)."""

    def test_table_entries_override_payload_shorthands_only(self):
        # Structural: the table exists to override a payload-table reading.
        # An entry outside the payload shorthands is dead weight that masks
        # where the real gap is.
        payload_shorts = {
            name
            for name in KUBECTL_DATA_PAYLOAD_FLAGS
            if not name.startswith("--")
        }
        for sub, shorts in KUBECTL_SUBCOMMAND_BOOLEAN_SHORTHANDS.items():
            assert shorts <= payload_shorts, (sub, shorts - payload_shorts)

    def test_logs_short_p_is_previous_not_patch(self):
        p = parse_command(["kubectl", "logs", "-p", "mypod", "-n", "ns"])
        assert p.positional_args == ("mypod",)
        assert "mypod" in p.host_relevant_tokens()
        assert p.data_payload_values == ()

    def test_logs_own_synopsis_resolves(self):
        # kubectl's own example: ``kubectl logs -p -c ruby web-1``.
        p = parse_command(["kubectl", "logs", "-p", "-c", "ruby", "web-1"])
        assert p.positional_args == ("web-1",)
        assert ("-c", "ruby") in p.flags

    def test_short_p_elsewhere_is_still_a_value(self):
        # The resolution must not leak: ``--patch`` everywhere else.
        p = parse_command(["kubectl", "patch", "pod", "x", "-p", '{"spec":{}}'])
        assert p.data_payload_values == ('{"spec":{}}',)
        p = parse_command(["kubectl", "patch", "pod", "x", "--patch", "{}"])
        assert p.data_payload_values == ("{}",)


class TestNoOptDefValAndGlobalArity:
    """R49: the round that audited the R45-R48 fixes themselves. Two faces
    the forward drift test cannot see:

    1. REVERSE face — a boolean-table long name whose help marker is a
       VALUE default (``--dry-run='none'``) is a string-typed flag read as
       valueless. That is correct only because pflag's NoOptDefVal
       semantics match the boolean reading in all three forms (bare /
       space / eq) — measured against the real binary, registered in
       KUBECTL_NOOPT_DEFVAL_FLAGS. Anything outside the whitelist is a
       wrong-present suspect and must be measured before admission.
    2. GLOBAL arity before the verb — a global value flag must swallow its
       value so the real verb keeps the subcommand slot. A global read as
       boolean lets the VALUE steal the slot and mis-keys every
       subcommand-scoped rule (R48's logs shorthand table included).
    """

    def test_boolean_table_value_markers_are_nooptdefval(self):
        if shutil.which("kubectl") is None:
            pytest.skip("kubectl not on PATH")
        suspects: list[tuple[str, str, str]] = []
        for sub in _HELP_SUBS:
            argv = ["kubectl", sub] + ([] if sub == "options" else ["--help"])
            text = subprocess.run(
                argv, capture_output=True, text=True, check=False
            ).stdout
            for line in text.splitlines():
                m = _HELP_FLAG_LINE.match(line)
                if m is None or m.group(2) not in KUBECTL_BOOLEAN_FLAGS:
                    continue
                marker = m.group(3)
                if marker is None or marker in ("false", "true"):
                    continue  # genuine boolean declaration
                if m.group(2) not in KUBECTL_NOOPT_DEFVAL_FLAGS:
                    suspects.append((sub, m.group(2), marker))
        assert not suspects, (
            "boolean-table members declared with a value marker outside "
            f"the measured NoOptDefVal whitelist: {suspects}"
        )

    def test_nooptdefval_three_forms_match_pflag(self):
        # bare: valueless, nothing to swallow
        p = parse_command(["kubectl", "delete", "pod", "x", "--cascade"])
        assert ("--cascade", None) in p.flags
        assert p.positional_args == ("pod", "x")
        # space: pflag does NOT swallow (measured: ``--cascade background``
        # makes ``background`` a second positional resource name — the
        # server answered NotFound twice). The parser must agree, and the
        # token must stay host-scanned.
        p = parse_command(
            ["kubectl", "delete", "pod", "x", "--cascade", "background"]
        )
        assert ("--cascade", None) in p.flags
        assert p.positional_args == ("pod", "x", "background")
        assert "background" in p.host_relevant_tokens()
        # eq: the value is taken through the parser's eq_val branch
        p = parse_command(
            ["kubectl", "delete", "pod", "x", "--cascade=foreground"]
        )
        assert ("--cascade", "foreground") in p.flags
        assert p.positional_args == ("pod", "x")

    def test_global_value_flags_before_verb_keep_the_subcommand_slot(self):
        if shutil.which("kubectl") is None:
            pytest.skip("kubectl not on PATH")
        text = subprocess.run(
            ["kubectl", "options"], capture_output=True, text=True, check=False
        ).stdout
        checked = 0
        for line in text.splitlines():
            m = _HELP_FLAG_LINE.match(line)
            if m is None:
                continue
            marker = m.group(3)
            if marker is None or marker in ("false", "true"):
                continue  # boolean global: nothing to swallow
            p = parse_command(["kubectl", m.group(2), "VAL", "get", "pods"])
            assert p.subcommand == "get", (m.group(2), p.subcommand)
            checked += 1
        # persistent globals that live on ``kubectl --help``, not options
        for flag in ("--namespace", "--server", "--v", "-n", "-s", "-v"):
            p = parse_command(["kubectl", flag, "VAL", "get", "pods"])
            assert p.subcommand == "get", (flag, p.subcommand)
            checked += 1
        assert checked >= 25, f"options page shrank? checked={checked}"
