"""Tests for guard_parser — AST-level kubectl/blade command parsing."""

import re
import shutil
import subprocess

import pytest

from chaos_agent.tools.guard import ToolGuard
from chaos_agent.tools.guard_parser import (
    BLADE_BOOLEAN_FLAGS,
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
# a widening there should show up as a deliberate diff here. R50: the
# coverage requirement is ONE-DIRECTIONAL — the admit set must be COVERED
# (an admitted subcommand with nobody drift-checking its boolean
# vocabulary is the dangerous direction), while scanning EXTRA names is
# harmless (the flat table applies to every kubectl parse; ``attach`` is
# scanned though not admitted). Parent commands (top/rollout/auth/config/
# cluster-info/set) have no flag section of their own; cordon/uncordon
# genuinely declare no boolean (their flags are NoOptDefVal/value-typed).
# ``options`` is the global-flag page (no --help suffix).
_HELP_SUBS = (
    "get", "describe", "delete", "replace", "exec", "logs", "top",
    "patch", "set", "scale", "debug", "wait", "cordon", "uncordon",
    "taint", "label", "annotate", "drain", "apply", "create",
    "rollout", "version", "cluster-info", "api-resources", "explain",
    "auth", "config", "run", "attach", "options",
)

# R50: scan-yield FLOORS. The drift tests assert ABSENCE of uncovered
# names — but a regex/format mismatch (kubectl changes how it prints flag
# lines) would silently collapse the scan to nothing and turn "0
# uncovered" into a fake green: the very false-negative class R49 hit
# with the EOL-colon variant of ``:\s``. Both floors are measured
# snapshots (R50): removing a boolean flag or drifting the format drops
# the yield below the floor and fails loudly; ADDING flags stays green.
# Re-derive and update when kubectl legitimately evolves. Both faces
# count GENUINE boolean declarations (no-eq and false/true markers): the
# 21 NoOptDefVal value-marker declarations (dry-run/cascade/validate)
# sit outside both counters, hence 158 total member declarations = 137
# + 21 on the reverse face.
_HELP_SCAN_FLOOR = 137      # boolean declarations matched across _HELP_SUBS
_REVERSE_SCAN_FLOOR = 137   # boolean-TABLE member declarations matched


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
        """Regression: `--` outside exec/run/debug MUST NOT split
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
        # R52: the former BLADE_SUBCOMMANDS "known set" is deleted — zero
        # production consumers and one tautological assertion here. The
        # subcommand EXTRACTION itself is the surviving contract.
        for sub in ("create", "destroy", "status", "prepare", "revoke"):
            p = parse_command(["blade", sub])
            assert p.subcommand == sub

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
        scanned = 0
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
                scanned += 1
                for name in (m.group(2), m.group(1)):
                    if name and name not in covered:
                        missing.append((sub, name))
        assert scanned >= _HELP_SCAN_FLOOR, (
            f"scan yield collapsed: {scanned} < {_HELP_SCAN_FLOOR} — format "
            "drift (the R49 EOL-colon class) or removed flags; re-derive "
            "the floor before trusting the coverage verdict below"
        )
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
        scanned = 0
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
                    scanned += 1
                    continue  # genuine boolean declaration
                if m.group(2) not in KUBECTL_NOOPT_DEFVAL_FLAGS:
                    suspects.append((sub, m.group(2), marker))
        assert scanned >= _REVERSE_SCAN_FLOOR, (
            f"reverse-face scan yield collapsed: {scanned} < "
            f"{_REVERSE_SCAN_FLOOR} — the whitelist verdict below is not "
            "backed by a live scan; re-derive the floor"
        )
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


class TestDriftScanIntegrity:
    """R50: the round that audited the R48/R49 fixes THEMSELVES. The drift
    suite's remaining silent-failure channels, closed:

    1. COVERAGE DIRECTION — ``_HELP_SUBS`` must cover the guard's admit
       set; an admitted subcommand whose boolean vocabulary nobody walks
       is the dangerous direction. Scanning extra names is harmless.
    2. PAYLOAD LONG axis — R48's "logs is the only colliding subcommand"
       was derived on the SHORT axis; a payload-table LONG name that is
       boolean under some subcommand would swallow the next token AND
       payload-skip it there (the measured ``-p`` escape chain). R50's
       enumeration: 0 hits on the long axis; the short axis re-derives
       exactly the handled logs pair.
    """

    def test_help_subs_cover_admit_set(self):
        admit = set(ToolGuard().kubectl_subcommands)
        uncovered = admit - set(_HELP_SUBS)
        assert not uncovered, (
            "admitted subcommands with no boolean drift scan: "
            f"{sorted(uncovered)} — add them to _HELP_SUBS"
        )
        assert "options" in _HELP_SUBS  # the global-flag page stays walked

    def test_payload_names_never_boolean_outside_the_handled_pair(self):
        if shutil.which("kubectl") is None:
            pytest.skip("kubectl not on PATH")
        payload_longs = {
            n for n in KUBECTL_DATA_PAYLOAD_FLAGS if n.startswith("--")
        }
        payload_shorts = {
            n for n in KUBECTL_DATA_PAYLOAD_FLAGS if not n.startswith("--")
        }
        hits: list[tuple[str, str, str]] = []
        for sub in _HELP_SUBS:
            argv = ["kubectl", sub] + ([] if sub == "options" else ["--help"])
            text = subprocess.run(
                argv, capture_output=True, text=True, check=False
            ).stdout
            handled_shorts = KUBECTL_SUBCOMMAND_BOOLEAN_SHORTHANDS.get(sub, ())
            for line in text.splitlines():
                m = _HELP_FLAG_LINE.match(line)
                if m is None:
                    continue
                marker = m.group(3)
                if marker is not None and marker not in ("false", "true"):
                    continue
                if m.group(2) in KUBECTL_NOOPT_DEFVAL_FLAGS:
                    continue  # boolean by measurement, not a collision
                if m.group(2) in payload_longs:
                    hits.append((sub, m.group(2), "long"))
                if m.group(1) and m.group(1) in payload_shorts:
                    if m.group(1) not in handled_shorts:
                        hits.append((sub, m.group(1), "short-unhandled"))
        assert not hits, (
            "payload-table names boolean under some subcommand outside the "
            f"handled shorthand pair (escape chain -p): {hits}"
        )


class TestDoubleDashUsageParity:
    """R51: the last table family with no mechanical reconciliation. The
    ``--`` split is an EXEMPTION surface (tokens after it become
    ``container_command`` instead of host-scanned positionals), so a member
    whose subcommand does not actually support the form lets the exemption
    cover a shape kubectl itself refuses — the dangerous direction (a
    missed member is merely over-deny). Membership is reconciled BOTH ways
    against the real binary: every member's help shows a
    ``kubectl <sub> ... -- ...`` usage/example line, and every subcommand
    whose help shows that shape is a member. ``attach`` was REMOVED (R51):
    no ``--`` shape in its help, and the client rejects the form before
    connecting (measured: "expected POD... saw 3").
    """

    _USAGE_DASH = re.compile(r"^\s*kubectl\s+\S+.*\s--\s")
    # R52: the reverse face is an ABSENCE assertion — without a scan-yield
    # floor a regex/format drift silently collapses the scan to zero and
    # turns "0 non-members" into a fake green (the R50 lesson recurring on
    # the R51 tooth). Measured floor: exec/run/debug carry the shape.
    _USAGE_SHAPE_FLOOR = 3

    def _usage_dash_count(self, sub: str) -> int:
        if shutil.which("kubectl") is None:
            pytest.skip("kubectl not on PATH")
        text = subprocess.run(
            ["kubectl", sub, "--help"], capture_output=True, text=True,
            check=False,
        ).stdout
        return sum(
            1 for line in text.splitlines() if self._USAGE_DASH.match(line)
        )

    def test_every_member_shows_the_usage_shape(self):
        shapeless = [
            sub for sub in sorted(KUBECTL_DOUBLE_DASH_SUBCOMMANDS)
            if self._usage_dash_count(sub) == 0
        ]
        assert not shapeless, (
            f"table members with no '--' usage shape in kubectl help: "
            f"{shapeless} — the container-command exemption would cover a "
            "form kubectl refuses; remove them"
        )

    def test_every_usage_shape_is_a_member(self):
        shapeful = 0
        unlisted = []
        for sub in _HELP_SUBS:
            if sub == "options":
                continue
            if self._usage_dash_count(sub) > 0:
                shapeful += 1
                if sub not in KUBECTL_DOUBLE_DASH_SUBCOMMANDS:
                    unlisted.append(sub)
        assert shapeful >= self._USAGE_SHAPE_FLOOR, (
            f"usage-shape scan yield collapsed: {shapeful} < "
            f"{self._USAGE_SHAPE_FLOOR} — the _USAGE_DASH regex no longer "
            "matches kubectl's help format; the reverse parity below would "
            "be a fake green"
        )
        assert not unlisted, (
            f"subcommands with a '--' usage shape missing from the table: "
            f"{unlisted} — their delegated command would be host-scanned "
            "and refused (over-deny); add them"
        )

    def test_attach_double_dash_is_no_longer_an_exemption(self):
        # R51 regression pin: attach left the table, so the `--` (and what
        # follows) falls back to the host-scanned positional stream —
        # exactly the shape kubectl itself rejects with "saw 3".
        p = parse_command(["kubectl", "attach", "pod1", "--", "echo", "hi"])
        assert p.container_command == ()
        assert "echo" in p.positional_args and "hi" in p.positional_args


class TestBladePolarityReconciliation:
    """R52: the BLADE flag tables reconciled against the installed
    v1.9.0-alpha fork (read-only help sweep + decisive binary probes).

    Measured facts pinned here (bare-form probes, R52 post-review):
    - ``-v`` is VALUE-taking where it lives (bare ``blade -v version`` →
      ``invalid value "version" for flag -v``) but its visibility FOLLOWS
      cobra's command tree: on the status chain the identical bare form
      dies with ``unknown shorthand flag: 'v' in -v``. Both real forms
      fail at the binary; the parser mirrors the value-taking reading.
    - every ADDED ``BLADE_VALUE_FLAGS`` member answered bare-form probes
      with ``flag needs an argument`` (destroy 5, status 4, k8s chain 1
      plus the create-k8s pair) — value-taking is measured, not read off
      the usage page (whose flag family cobra may never merge).
    - the fork's destroy page carries 5 value flags that were missing from
      ``BLADE_VALUE_FLAGS`` — the provenance walk handed the CLUSTER uuid
      to the UID shape gate, got it rejected → "" → the REAL experiment
      uid was skipped → fail-closed false refusal of the task's own
      cleanup (round-14 F3's ``--kubeconfig`` class, fork-flag instance).
    - ghosts (``--debug``/``--version``/``--no-color``) stay boolean on
      purpose: the binary refuses them anyway, and a boolean reading only
      widens the host-scanned positional stream.
    """

    def test_v_absorbs_its_value_not_the_subcommand(self):
        # Parser view under the value-reading; on the REAL binary this
        # spelling dies at the root (unknown shorthand — the flag lives on
        # a subcommand's persistent set), so the admitted-view direction
        # is harmless either way.
        p = parse_command(["blade", "-v", "3", "status"])
        assert p.subcommand == "status"
        assert ("-v", "3") in p.flags

    def test_v_bare_form_leaves_no_subcommand(self):
        # Real binary: ``blade -v version`` dies on the invalid int; the
        # parser now mirrors that (version absorbed as the level).
        p = parse_command(["blade", "-v", "version"])
        assert p.subcommand is None
        assert ("-v", "version") in p.flags

    def test_destroy_cluster_uuid_no_longer_hijacks_the_uid(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            destroy_uid_from_tokens,
        )

        cluster = "c62735cce1d61445995c0f1d9e4a1bded"  # 32-hex, real shape
        uid = "aabbccddeeff0011"                       # HEX16 UID shape
        assert destroy_uid_from_tokens(
            ["--cluster-uuid", cluster, uid]) == uid

    def test_ghost_members_stay_boolean(self):
        # ``--debug`` is refused by the binary; the parser keeps the
        # boolean reading (never swallows the next token) — recorded
        # decision, pinned so a polarity flip cannot happen silently.
        p = parse_command(["blade", "--debug", "status"])
        assert p.subcommand == "status"
        assert ("--debug", None) in p.flags
