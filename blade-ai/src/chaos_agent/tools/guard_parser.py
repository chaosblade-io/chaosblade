"""AST-level command parser for ToolGuard.

Splits a ``cmd: list[str]`` (exec-form, never passed to a shell) into a
structured ``ParsedCommand`` so that ``ToolGuard`` can:
  - Identify the subcommand without hand-rolled while-loops over flags.
  - Skip shell-pattern checks on data-payload flag values (e.g.
    ``-p JSON``, ``--from-literal k=v``) — these are opaque data to
    the binary, not shell commands.
  - Skip shell-pattern checks on tokens after ``--`` for kubectl
    exec/run/attach/debug — those run inside the container.
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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

# kubectl boolean flags — explicit list REQUIRED. The parser's fallback
# treats an unknown flag as value-taking (consumes the next token), so
# omitting a boolean here causes the next positional to be silently
# swallowed. Covers global + frequently-used per-subcommand booleans.
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
    "--cascade",          # technically takes value in newer kubectl; safer as bool
    "--prune",
    "--validate",
    "--dry-run",          # newer kubectl wants =client|server but bare form still parses
    # exec/run
    "-i", "--stdin",
    "-t", "--tty",
    # logs — note: -f/--follow is subcommand-dependent (boolean in
    # ``kubectl logs``, but ``kubectl get -f file.yaml`` uses -f as
    # --filename). Defaulting to value-taking is the safer choice for
    # the security guard: in the logs case, the next positional may be
    # mis-classified as the -f value, but it still lands in
    # host_relevant_tokens() and is checked. In the get/apply case,
    # the filename correctly lands in data_payload_values and is
    # skipped from regex checks.
    "--previous",
    "--prefix",
    "--timestamps",
    # describe
    "--show-events",
    # rollout etc.
    "--allow-missing-template-keys",
})

# kubectl flags whose value is opaque data (JSON, label string, file
# path, selector expression) — not a shell command. These get put in
# data_payload_values and skipped by host_relevant_tokens().
KUBECTL_DATA_PAYLOAD_FLAGS: frozenset[str] = frozenset({
    "-p", "--patch",
    "-f", "--filename",                  # also boolean for logs; resolved by subcommand context if needed
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
KUBECTL_DOUBLE_DASH_SUBCOMMANDS: frozenset[str] = frozenset({
    "exec", "run", "attach", "debug",
})

# blade boolean flags — same rationale as KUBECTL_BOOLEAN_FLAGS.
BLADE_BOOLEAN_FLAGS: frozenset[str] = frozenset({
    "-h", "--help",
    "-d", "--debug",
    "-v", "--version",
    "--no-color",
})

# blade value-taking flags (explicit list for documentation; the parser
# would default to value-taking anyway, but listing makes audit obvious).
BLADE_VALUE_FLAGS: frozenset[str] = frozenset({
    "--time", "--interface", "--names", "--namespace", "--container",
    "--labels", "--percent", "--rate", "--offset", "--port", "--protocol",
    "--remote-port", "--local-port", "--exclude-port", "--target", "--type",
    "--kubeconfig", "--cri-endpoint", "--container-runtime",
    "--uid", "--ip", "--hostname", "--domain", "--device", "--mode",
})

BLADE_SUBCOMMANDS: frozenset[str] = frozenset({
    "create", "destroy", "status", "prepare", "revoke",
    "query", "version", "help",
})

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
    # Tokens after the `--` separator for exec/run/attach/debug. Fully
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
        # when we're inside an exec/run/attach/debug subcommand. In
        # other contexts it's a plain positional (defense-in-depth: a
        # misplaced ``--`` must not become a host-check bypass).
        if token == "--":
            if subcommand in KUBECTL_DOUBLE_DASH_SUBCOMMANDS:
                container_cmd = list(cmd[i + 1:])
                break
            # Outside exec/run/attach/debug: treat `--` as a positional
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
            # Boolean flag — no value, no token consumption beyond self
            if name in KUBECTL_BOOLEAN_FLAGS and eq_val is None:
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


_PARSERS: dict[str, Callable[[list[str]], ParsedCommand]] = {
    "kubectl": _parse_kubectl,
    "blade": _parse_blade,
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
