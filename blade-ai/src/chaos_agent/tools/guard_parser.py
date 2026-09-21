"""AST-level command parser for ToolGuard.

Splits a ``cmd: list[str]`` (exec-form, never passed to a shell) into a
structured ``ParsedCommand`` so that ``ToolGuard`` can:
  - Identify the subcommand without hand-rolled while-loops over flags.
  - Skip shell-pattern checks on data-payload flag values (e.g.
    ``-p JSON``, ``--from-literal k=v``) — these are opaque data to
    the binary, not shell commands.
  - Skip shell-pattern checks on tokens after ``--`` for kubectl
    exec/run/debug — those run inside the container.
  - Reject suspicious solo shell-metachar tokens (``;``, ``|``, ``&``,
    ``>``, ``<``, ``&&``, ``||``) regardless of position, as
    defense-in-depth against anomalous LLM output (exec-form would
    treat them as literal strings, but their presence signals the LLM
    *intended* shell syntax).

Design constraints:
  - Pure stdlib (no argparse / click / shlex); kubectl/blade flag
    grammars cannot be expressed by argparse anyway.
  - ``parse_command`` never raises — unknown binaries fall back to
    "binary + all-positional" so every token still enters host-relevant
    checks downstream.
  - Schemas are exposed at module level for runtime extension via
    monkeypatch / subclass.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

# kubectl boolean flags — explicit list REQUIRED. The parser's fallback
# treats an unknown flag as value-taking (consumes the next token), so
# omitting a boolean here causes the next positional to be silently
# swallowed — and when the flag precedes the verb, the SWALLOWED token is
# the SUBCOMMAND itself (``guard.py``'s allowlist is keyed on the first
# non-flag token), so the shape check then runs against the wrong verb.
# Coverage is mechanical, never "frequently used": the table is diffed
# against the synopsis of EVERY admitted subcommand plus ``kubectl
# options`` (R47 — 42 members were missing and were added; R48 re-ran the
# same diff at MEMBER level and added ``--interactive``, whose covered
# shorthand ``-i`` had masked it from R47's set-level comparison. The diff
# is re-runnable and pinned by the drift test in test_guard_parser.py).
KUBECTL_BOOLEAN_FLAGS: frozenset[str] = frozenset({
    # Global / output
    "-h", "--help", "--version",
    "-A", "--all-namespaces", "--all",
    "-w", "--watch", "--watch-only",
    "-R", "--recursive",
    "--no-headers", "--show-labels", "--show-kind", "--show-managed-fields",
    "--server-print",
    "--insecure-skip-tls-verify",
    "-q", "--quiet",
    # get/delete/wait
    "--ignore-not-found",
    "--force",
    "--cascade",          # NoOptDefVal string — KUBECTL_NOOPT_DEFVAL_FLAGS
    "--prune",
    "--validate",         # NoOptDefVal string — KUBECTL_NOOPT_DEFVAL_FLAGS
    "--dry-run",          # NoOptDefVal string — KUBECTL_NOOPT_DEFVAL_FLAGS
    # exec/run
    "-i", "--stdin",
    "-t", "--tty",
    # logs — ``--follow`` is a boolean here; its shorthands ``-f``/``-p``
    # are the subcommand-dependent pair (``--filename``/``--patch``
    # elsewhere) resolved by subcommand in the parser via
    # KUBECTL_SUBCOMMAND_BOOLEAN_SHORTHANDS, not by this flat table. The
    # reasoning that used to sit here — "reading -f as value-taking is safe
    # because the swallowed token still lands in host_relevant_tokens() and
    # is checked" — was FALSE: ``-f`` is ALSO a member of
    # KUBECTL_DATA_PAYLOAD_FLAGS, so the swallowed token was
    # payload-skipped and escaped every host check (R47, measured for
    # ``-f``; R48 found the second member ``-p`` that R47's "the ONE"
    # claim had missed, and closed the class mechanically).
    "--follow",
    "--all-containers",
    "--all-pods",
    "--ignore-errors",
    "--insecure-skip-tls-verify-backend",
    "--previous",
    "--prefix",
    "--timestamps",
    # describe
    "--show-events",
    # rollout etc.
    "--allow-missing-template-keys",
    # run
    "--privileged",
    "--rm",
    "--expose",
    "--leave-stdin-open",
    "--command",
    "--attach",
    # debug
    "--arguments-only",
    "--replace",
    "--same-node",
    "--share-processes",
    "--keep-annotations",
    "--keep-init-containers",
    "--keep-labels",
    "--keep-liveness",
    "--keep-readiness",
    "--keep-startup",
    # drain
    "--ignore-daemonsets",
    "--disable-eviction",
    "--delete-emptydir-data",
    # delete / apply / create / replace / label / patch / taint
    "--now",
    "--wait",
    "--interactive",      # delete ``-i`` — R48: masked from the set-level
                          # diff because ``-i`` was already covered (as
                          # exec's ``--stdin`` shorthand)
    "--server-side",
    "--force-conflicts",
    "--openapi-patch",
    "--edit",
    "--windows-line-endings",
    "--save-config",
    "--list",
    "--local",
    "--overwrite",
    # api-resources / version / get / global
    "--cached",
    "--namespaced",
    "--client",
    "--output-watch-events",
    "--disable-compression",
    "--match-server-version",
    "--warnings-as-errors",
})

# String-typed flags that pflag declares with NoOptDefVal: they ACCEPT a
# bare occurrence (``--dry-run`` == ``--dry-run=client``, deprecated but
# still parsed) and — the pflag semantics that decides their table home —
# in ``--flag value`` SPACE form they do NOT swallow the next token; the
# value must be glued (``--flag=value``). Measured against kubectl v1.34.1
# (R49, the round that audited the R45-R48 fixes themselves):
#   delete pod x --dry-run             -> dry run executes (deprecation warn)
#   delete pod x --cascade background  -> BOTH ``x`` and ``background`` are
#                                         positional resource names
#                                         (server NotFound twice)
#   apply -f /dev/null --validate strict -> "Unexpected args: [strict]"
# So the flat boolean table's valueless reading matches pflag in all three
# forms: bare (no token to swallow), space (the would-be value stays a
# positional and is host-scanned), and ``=`` (the parser's eq_val branch
# takes it as a value). A help marker that is not ``false``/``true`` does
# NOT make these wrong-present in the boolean table — the reverse-face
# drift test keys on this whitelist, and any new member must be measured
# the same way (bare + space + eq against the real binary) before being
# added here.
KUBECTL_NOOPT_DEFVAL_FLAGS: frozenset[str] = frozenset({
    "--dry-run",     # help marker ='none'; bare form deprecated but parsed
    "--cascade",     # help marker ='background'
    "--validate",    # help marker ='strict'
})

# kubectl flags whose value is opaque data (JSON, label string, file
# path, selector expression) — not a shell command. These get put in
# data_payload_values and skipped by host_relevant_tokens().
KUBECTL_DATA_PAYLOAD_FLAGS: frozenset[str] = frozenset({
    "-p", "--patch",                     # ``logs -p`` is ``--previous`` (boolean) —
                                         # resolved by subcommand in the parser
    "-f", "--filename",                  # --filename semantics (get/apply/exec…);
                                         # ``logs -f`` is ``--follow`` (boolean) —
                                         # resolved by subcommand in the parser
    "--from-literal", "--from-file", "--from-env-file",
    "--annotation", "--annotations",
    "--labels", "--label",
    "--data", "--data-binary",
    "-l", "--selector",
    "--field-selector",
    "--overrides",
    # Cluster / auth config flags — their values are paths, URLs, or
    # opaque tokens (never shell commands). Listing them here skips
    # the shell-pattern regex on the value, eliminating false positives
    # on legitimate but unusual paths like ``/tmp/foo;bar/kubeconfig``.
    "--kubeconfig",
    "--context",
    "--cluster",
    "--user",
    "--server",
    "--token",
    "--certificate-authority",
    "--client-certificate",
    "--client-key",
    "--as",
    "--as-group",
    "--as-uid",
})

# kubectl subcommands where `--` separates host args from a command
# delegated to a container/process. Outside these subcommands, `--`
# is treated as a plain positional token (so a misplaced `--` cannot
# become a host-check bypass).
# R51: membership is MECHANICALLY verified against the real binary —
# every member's ``kubectl <sub> --help`` shows a ``kubectl <sub> ... -- ...``
# usage/example line (exec 7, run 3, debug 2), and every subcommand whose
# help shows that shape must be a member. ``attach`` was removed here:
# its help carries NO ``--`` shape (Usage: ``kubectl attach (POD |
# TYPE/NAME) -c CONTAINER [options]``), and the client rejects the form
# before connecting — measured: ``kubectl attach pod1 -- echo hi`` →
# "error: expected POD, TYPE/NAME, or TYPE NAME, (at most 2 arguments)
# saw 3: [pod1 echo hi]" (``--`` swallowed by pflag, both words became
# surplus positionals), while ``attach pod1 -i`` reaches the server.
# Cross-checked on the wiz channel with a DIFFERENT kubectl build
# (executor v1.30.0 vs local v1.34.1): the same nonexistent-pod probe
# reproduces the IDENTICAL "saw 3" rejection while ``exec`` on the same
# pod name reaches the server (NotFound) — the removal holds on the
# production path, not just locally.
# Keeping attach would let the container-command exemption cover tokens
# for a form kubectl itself refuses — the dangerous direction of this
# table (a missed member is merely over-deny).
KUBECTL_DOUBLE_DASH_SUBCOMMANDS: frozenset[str] = frozenset({
    "exec", "run", "debug",
})

# Subcommand-dependent boolean SHORTHANDS — the one place the flat tables
# cannot hold the truth. A shorthand that is a BOOLEAN under one subcommand
# collides with a PAYLOAD-table shorthand elsewhere (``logs -f`` is
# ``--follow`` while ``-f`` is ``--filename`` everywhere else; ``logs -p``
# is ``--previous`` while ``-p`` is ``--patch``): the parser's flat-boolean
# branch is subcommand-blind, the payload table is subcommand-blind, and a
# shorthand read as value-taking SWALLOWS the next token AND payload-skips
# it — the token then escapes every host check (R47 measured the ``-f``
# escape; R48 found ``-p``).
#
# R47 resolved ``-f`` with a parser literal and declared it "the ONE
# genuinely subcommand-dependent spelling". That claim was never
# mechanically testable as written: R47's dependency scan compared LONG
# names (``--follow`` vs ``--filename``), while the collision lives in the
# SHORT ones — and R47's own drift test missed ``-p`` for the same reason
# (its missing-check was set-level: the covered long twin ``--previous``
# masked the uncovered shorthand).
#
# This table is the MECHANICAL result, re-derivable: for every subcommand
# ``guard.py`` admits, intersect that subcommand's own boolean shorthands
# (parsed from ``kubectl <sub> --help``) with the payload table's
# shorthands. ``logs`` is the only subcommand that collides, and
# ``-f``/``-p`` are its only members. The drift test in
# test_guard_parser.py re-runs the scan at NAME level (every name of every
# boolean declaration must be covered) and is skipped where kubectl is
# absent.
KUBECTL_SUBCOMMAND_BOOLEAN_SHORTHANDS: dict[str, frozenset[str]] = {
    "logs": frozenset({"-f", "-p"}),
}

# blade boolean flags — polarity MEASURED against the installed binary
# (v1.9.0-alpha kubewiz fork, R52; read-only help surfaces only):
#   -h/--help: built-in — ``blade --help`` prints usage and exits, never
#     consumes a value. Measured alive.
#   -d/--debug, --version, --no-color: GHOSTS — "flag provided but not
#     defined" on the real binary. Kept anyway: a boolean reading only
#     widens the host-scanned positional stream and the command fails at
#     the binary regardless — the safe direction. Recorded, not assumed.
#   -v: REMOVED — measured on BOTH spellings (bare-form probes):
#     ``blade -v version`` → ``invalid value "version" for flag -v`` (the
#     flag is VALUE-taking int where it lives), but ``blade -v 3 status``
#     → ``unknown shorthand flag: 'v' in -v`` — its visibility FOLLOWS
#     cobra's command tree (a subcommand's persistent set, not root's).
#     Both real forms fail at the binary, so either polarity is harmless;
#     the boolean row additionally mis-keyed the subcommand slot where the
#     flag lives (``blade -v version`` read "version" as the subcommand).
#   USAGE-PAGE ≠ PARSE-FACE (R52 lesson): the top-level help page prints a
#     klog flag family (-logtostderr, -v, -log_dir, ...) that cobra never
#     merged into the parser — every spelling is refused ("unknown
#     shorthand flag: 'l' in -logtostderr" / "unknown flag: --v"). No klog
#     member was added to this table; the kubectl equivalence (help page =
#     parse face) does NOT hold for the cobra family.
BLADE_BOOLEAN_FLAGS: frozenset[str] = frozenset({
    "-h", "--help",
    "-d", "--debug",
    "--version",
    "--no-color",
})

# blade value-taking flags (explicit list; the parser defaults to
# value-taking anyway — the CONSUMING face is the provenance walk in
# verify.py ``destroy_uid_from_tokens``, where a value flag MUST be known
# or its value is mistaken for the experiment UID). Reconciled against the
# installed v1.9.0-alpha fork, R52 (read-only help sweep, 11 pages incl.
# the action layer):
#   + ``--cluster-uuid``/``--kubectl-proxy``/``--kubewiz-token``/
#     ``--kubewiz-url``/``--token``: the fork's destroy page carries 8
#     value flags and 5 of these were MISSING — ``blade destroy
#     --cluster-uuid <c-uuid> <uid>`` made the walk hand the CLUSTER uuid
#     to the UID shape gate, which rejected it → "" → the REAL uid was
#     skipped → fail-closed FALSE REFUSAL of the task's own cleanup (the
#     same class as round-14 F3's ``--kubeconfig``).
#   + ``--action``/``--flag-filter``/``--limit``/``--status`` (status page)
#     and ``--waiting-time`` (k8s chain): value-real, same walk-safety
#     boundary, zero cost to list.
#   KNOWN DRIFT (recorded, kept; narrowed by R53's host-chain re-sweep):
#     ``--interface``/``--protocol``/``--local-port``/``--remote-port``/
#     ``--exclude-port``/``--target``/``--type`` are ABSENT from the k8s
#     pages R52 swept AND from the host ``network drop`` page — re-verified
#     in R53 both by help grep AND by reading the page's FULL local-flag
#     list (none of the seven appears). The other members once lumped
#     under this drift line were MEASURED ALIVE on host-level chains in
#     R53: bare-form "needs an argument" for ``--time`` (python/strace
#     delay), ``--offset`` (time travel), ``--port`` (network occupy);
#     ``=value --help`` eq-form probe for ``--percent`` (disk fill) and
#     ``--rate`` (mem load) — the eq form parses a value flag and prints
#     help, while a boolean dies with a ParseBool error (both outcomes
#     stay at the parse/help layer, never reaching Run) — the R52 blanket
#     claim "never appear on any fork help page" was TOO WIDE: that sweep only
#     covered the k8s chain. Ghost membership is walk-cosmetic here — the
#     consuming face is the destroy/revoke tail (``destroy_uid_from_tokens``
#     ), whose flags were reconciled directly; a ghost member only makes
#     the walk skip a token after a flag the binary does not define on the
#     destroy chain — harmless direction.
BLADE_VALUE_FLAGS: frozenset[str] = frozenset({
    "--time", "--interface", "--names", "--namespace", "--container",
    "--labels", "--percent", "--rate", "--offset", "--port", "--protocol",
    "--remote-port", "--local-port", "--exclude-port", "--target", "--type",
    "--kubeconfig", "--cri-endpoint", "--container-runtime",
    "--uid", "--ip", "--hostname", "--domain", "--device", "--mode",
    "--cluster-uuid", "--kubectl-proxy", "--kubewiz-token",
    "--kubewiz-url", "--token", "--action", "--flag-filter",
    "--limit", "--status", "--waiting-time",
})

# R52: the former BLADE_SUBCOMMANDS frozenset is deleted — it had ZERO
# production consumers (_parse_blade takes the first non-flag token as the
# subcommand without any set check) and one tautological test assertion;
# a dead table reads as a claim ("subcommands are validated") that no code
# honors. The subcommand vocabulary now lives only in the real parser.

# Solo shell-metachar tokens — independent presence of these in cmd is
# anomalous LLM behavior (exec-form would treat them as literals; their
# presence signals the LLM intended shell syntax). Rejected outright
# by ToolGuard regardless of where they appear.
SUSPICIOUS_SOLO_TOKENS: frozenset[str] = frozenset({
    ";", "|", "&", "||", "&&", ">", "<", ">>", "<<", "<<<",
})


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedCommand:
    """Structured view of a cmd list after AST-level parsing."""
    binary: str
    subcommand: str | None
    positional_args: tuple[str, ...]
    # (flag_name, value_or_None). Order preserved; value None means
    # boolean flag (no value attached).
    flags: tuple[tuple[str, str | None], ...]
    # Values that were attached to data-payload flags. These are skipped
    # by host_relevant_tokens(). Stored as a tuple (not dict) because a
    # flag may legitimately appear multiple times (e.g. ``--from-literal``).
    data_payload_values: tuple[str, ...]
    # Tokens after the `--` separator for exec/run/debug. Fully
    # excluded from host_relevant_tokens() (runs inside the container).
    container_command: tuple[str, ...]

    def host_relevant_tokens(self) -> tuple[str, ...]:
        """Tokens that should be subjected to shell-pattern checks.

        Includes: binary, subcommand, positional_args, flag names, and
        non-payload flag values.

        Excludes: container_command (runs inside the pod) and
        data_payload_values (opaque data, not shell tokens).
        """
        out: list[str] = [self.binary]
        if self.subcommand:
            out.append(self.subcommand)
        out.extend(self.positional_args)
        for name, val in self.flags:
            out.append(name)
            if val is not None and val not in self.data_payload_values:
                out.append(val)
        return tuple(out)


# ---------------------------------------------------------------------------
# Per-binary parsers
# ---------------------------------------------------------------------------


def _split_flag_eq(token: str) -> tuple[str, str | None]:
    """``--foo=bar`` → (``--foo``, ``bar``). ``-x`` → (``-x``, None)."""
    if "=" in token and token.startswith("-"):
        name, val = token.split("=", 1)
        return name, val
    return token, None


def _parse_kubectl(cmd: list[str]) -> ParsedCommand:
    binary = "kubectl"
    subcommand: str | None = None
    positional: list[str] = []
    flags: list[tuple[str, str | None]] = []
    payloads: list[str] = []
    container_cmd: list[str] = []

    i = 1
    n = len(cmd)
    while i < n:
        token = cmd[i]

        # `--` separator handling: only treated as host/container split
        # when we're inside an exec/run/debug subcommand. In
        # other contexts it's a plain positional (defense-in-depth: a
        # misplaced ``--`` must not become a host-check bypass).
        if token == "--":
            if subcommand in KUBECTL_DOUBLE_DASH_SUBCOMMANDS:
                container_cmd = list(cmd[i + 1:])
                break
            # Outside exec/run/debug: treat `--` as a positional
            # rather than a flag (avoids consuming the next token as a
            # phantom value).
            if subcommand is None:
                subcommand = token
            else:
                positional.append(token)
            i += 1
            continue

        if token.startswith("-") and len(token) > 1:
            name, eq_val = _split_flag_eq(token)
            # Boolean flag — no value, no token consumption beyond self.
            # Shorthands that are boolean under THIS subcommand while the
            # flat payload table classifies them as values are resolved by
            # subcommand here: ``logs -f`` is ``--follow`` and ``logs -p``
            # is ``--previous``, while ``-f``/``-p`` are ``--filename``/
            # ``--patch`` everywhere else. Reading either as value-taking
            # under ``logs`` swallowed the next token AND payload-skipped
            # it, so the token escaped every host check (R47 measured the
            # ``-f`` escape; R48 closed the class with ``-p``).
            if (
                name in KUBECTL_BOOLEAN_FLAGS
                or name
                in KUBECTL_SUBCOMMAND_BOOLEAN_SHORTHANDS.get(subcommand or "", ())
            ) and eq_val is None:
                flags.append((name, None))
                i += 1
                continue
            # Value flag (explicit data payload or unknown — default to
            # value-taking). If `--foo=bar` syntax, value already in
            # eq_val; otherwise take next token if available.
            if eq_val is not None:
                flags.append((name, eq_val))
                if name in KUBECTL_DATA_PAYLOAD_FLAGS:
                    payloads.append(eq_val)
                i += 1
                continue
            # `--foo bar` syntax — peek next token as the value
            if i + 1 < n and not cmd[i + 1].startswith("-"):
                val = cmd[i + 1]
                flags.append((name, val))
                if name in KUBECTL_DATA_PAYLOAD_FLAGS:
                    payloads.append(val)
                i += 2
                continue
            # Flag at end of cmd or followed by another flag — record as
            # value-less (safer than guessing).
            flags.append((name, None))
            i += 1
            continue

        # First non-flag token is the subcommand.
        if subcommand is None:
            subcommand = token
            i += 1
            continue

        # Subsequent non-flag tokens are positional args.
        positional.append(token)
        i += 1

    return ParsedCommand(
        binary=binary,
        subcommand=subcommand,
        positional_args=tuple(positional),
        flags=tuple(flags),
        data_payload_values=tuple(payloads),
        container_command=tuple(container_cmd),
    )


def _parse_blade(cmd: list[str]) -> ParsedCommand:
    binary = "blade"
    subcommand: str | None = None
    positional: list[str] = []
    flags: list[tuple[str, str | None]] = []

    i = 1
    n = len(cmd)
    while i < n:
        token = cmd[i]

        if token.startswith("-") and len(token) > 1:
            name, eq_val = _split_flag_eq(token)
            if name in BLADE_BOOLEAN_FLAGS and eq_val is None:
                flags.append((name, None))
                i += 1
                continue
            if eq_val is not None:
                flags.append((name, eq_val))
                i += 1
                continue
            if i + 1 < n and not cmd[i + 1].startswith("-"):
                flags.append((name, cmd[i + 1]))
                i += 2
                continue
            flags.append((name, None))
            i += 1
            continue

        # blade subcommands: first non-flag token (create / destroy / ...)
        if subcommand is None:
            subcommand = token
            i += 1
            continue

        positional.append(token)
        i += 1

    return ParsedCommand(
        binary=binary,
        subcommand=subcommand,
        positional_args=tuple(positional),
        flags=tuple(flags),
        data_payload_values=(),       # blade has no opaque data flags worth excluding
        container_command=(),         # blade doesn't use `--` separator
    )


def _parse_generic(cmd: list[str]) -> ParsedCommand:
    """Fallback parser for binaries without a dedicated schema.

    All tokens after the binary are classified as positional_args so
    that host_relevant_tokens() still covers them. This ensures unknown
    binaries can never inadvertently bypass shell-pattern checks.
    """
    return ParsedCommand(
        binary=Path(cmd[0]).name,
        subcommand=None,
        positional_args=tuple(cmd[1:]),
        flags=(),
        data_payload_values=(),
        container_command=(),
    )


def _parse_wiz(cmd: list[str]) -> ParsedCommand:
    """Transparent unwrap for ``wiz task exec --command "<inner cmd>" ...``.

    After the transport-layer migration, ``build_kubectl_cmd`` no longer
    wraps with wiz — wrapping is done by ``KubewizK8sChannel.wrap_command()``.
    However, ``_parse_wiz`` is still needed for defense-in-depth: if a
    wiz-wrapped command reaches the guard (e.g. via ``run_command``
    without ``skip_guard=True``), this parser unwraps ``--command`` and
    re-parses the inner command with its OWN parser (kubectl/blade),
    lifting the inner structure up to the wiz level:
      - inner host-relevant tokens (the ``--``-prefix segment: binary,
        subcommand, positional args, flag names/values) stay CHECKED;
      - inner ``container_command`` (after ``--`` for exec/run/debug)
        stays EXEMPT.
    This makes kubewiz-wrapped commands behave IDENTICALLY to raw commands.

    The wiz shell itself (``task``/``exec``/``--cluster-uuid``/``--profile``)
    is builder-controlled (not LLM-controlled) and carries no shell payload,
    so it is not re-checked here. If ``--command`` is absent or its inner
    binary is unrecognized, falls back to ``_parse_generic`` (whole-cmd
    checks — safe default).
    """
    inner_str: str | None = None
    i = 1
    n = len(cmd)
    while i < n:
        tok = cmd[i]
        if tok == "--command" and i + 1 < n:
            inner_str = cmd[i + 1]
            break
        if tok.startswith("--command="):
            inner_str = tok.split("=", 1)[1]
            break
        i += 1

    if not inner_str:
        return _parse_generic(cmd)

    try:
        inner_tokens = shlex.split(inner_str)
    except ValueError:
        inner_tokens = inner_str.split()

    if not inner_tokens:
        return _parse_generic(cmd)

    inner_binary = Path(inner_tokens[0]).name
    inner_parser = _PARSERS.get(inner_binary)
    if inner_parser is None:
        # Unrecognized inner binary — keep the whole cmd under host checks.
        return _parse_generic(cmd)

    inner = inner_parser(inner_tokens)

    lifted = inner.host_relevant_tokens()
    # A semicolon glued INSIDE a lifted token (``pods;``) means the ORIGINAL
    # string carried shell-chaining syntax. After shlex.split, the per-token
    # blacklist patterns (``;\s*rm``, ``rm\s+-rf`` …) can no longer see across
    # the boundary, and the wiz transport re-parses the string through a remote
    # shell. Fall back to whole-command checks on the raw --command string
    # token, where those patterns still match. A legit wrapped command never
    # produces one: resource names cannot contain ';', and data payloads are
    # excluded from the lifted tokens.
    if any(";" in token for token in lifted):
        return _parse_generic(cmd)

    # Lift inner structure to the wiz level. ``inner.host_relevant_tokens()``
    # already excludes the inner container_command and data payloads, so
    # placing it in positional_args re-checks exactly what kubeconfig mode
    # would check — no more, no less.
    return ParsedCommand(
        binary="wiz",
        subcommand=None,
        positional_args=lifted,
        flags=(),
        data_payload_values=inner.data_payload_values,
        container_command=inner.container_command,
    )


_PARSERS: dict[str, Callable[[list[str]], ParsedCommand]] = {
    "kubectl": _parse_kubectl,
    "blade": _parse_blade,
    "wiz": _parse_wiz,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_command(cmd: list[str]) -> ParsedCommand:
    """Parse a cmd list into a structured ParsedCommand.

    Dispatches on ``Path(cmd[0]).name`` so that absolute paths like
    ``/usr/local/bin/kubectl`` parse the same as bare ``kubectl``.
    """
    if not cmd:
        # Defensive — ToolGuard.check rejects empty cmd before calling
        # this, but parser must still return a usable object.
        return ParsedCommand(
            binary="",
            subcommand=None,
            positional_args=(),
            flags=(),
            data_payload_values=(),
            container_command=(),
        )

    binary_name = Path(cmd[0]).name
    parser = _PARSERS.get(binary_name, _parse_generic)
    return parser(cmd)
