"""Shared read-only command classifier — the single source of truth for
"is this shell command a read-only probe?".

Three separate judgments used to answer this question with divergent
vocabularies:

  - ``k8s_native._is_readonly_exec_probe`` — ``detect()`` injection attribution.
  - ``classifier._classify_kubectl_exec`` — the guard SCOPE for the screeners.
  - ``_baseline_profiles.validate_command`` — ``host_read`` + baseline capture.

This module unifies them. The core per-command judgment
(:func:`is_readonly_argv`) is context-independent — ``iptables -L`` is a
read-only rule dump whether it runs inside a pod exec or directly on a host, so
the same judgment applies everywhere. Two thin adapters sit on top:

  - :func:`is_readonly_kubectl_exec` — parses ``POD [-n NS] [-c C] -- INNER``,
    unwraps one ``sh -c`` layer, splits pipelines, and classifies each stage
    (matches the former ``_is_readonly_exec_probe`` behaviour exactly, plus the
    dual-use arg guards below).
  - :func:`is_readonly_host_command` — a bare host command (no ``POD --``); a
    single read-only diagnostic with NO shell metacharacters (pipe / redirect /
    chain / substitution), matching the former host-profile
    ``validate_command`` policy.

Dual-use tools (``iptables`` / ``nft`` / ``tc`` / ``ip`` / ``systemctl`` /
``mount`` / ``dmesg``) are classified at the ARGUMENT level: their inspection
forms (``iptables -L``, ``ip addr show``, ``systemctl status``) are read-only
while their mutating forms (``iptables -A``, ``ip link set``, ``systemctl
stop``, ``dmesg -C``) are NOT. This closes a latent hole where a "read-only"
tool could run ``ip link set down`` / ``mount -o remount`` / ``dmesg -C``.

Every rejection carries a SPECIFIC reason (which binary / verb / operator made
it non-read-only) so a tool can tell the LLM exactly what to fix, rather than a
generic "rejected". The module is self-contained (no ``agent`` imports) so the
``tools`` layer can depend on it without an upward dependency.
"""

from __future__ import annotations

import re
import shlex

# One ``sh -c "<script>"`` wrapper is peeled to reach the real entry token
# (mirrors ``target_guard.carriers._host_entry_tokens``; replicated here so this
# module stays in the ``tools`` layer with no agent-package import). Nested
# shells beyond one layer are unusual for a probe and keep failing closed.
_SHELL_WRAPPERS = ("sh", "bash", "ash", "dash", "/bin/sh", "/bin/bash")

# Pure read-only diagnostics (no mutating form). Union of the k8s exec-probe
# vocabulary and the host baseline diagnostic whitelist. Dual-use binaries are
# NOT listed here — they get argument-level guards in ``_classify_argv``.
_READONLY_BINARIES = frozenset({
    # identity / capability inspection
    "which", "type", "command", "test", "[", "uname", "id", "hostname",
    "whoami", "getent", "env", "printenv", "nproc",
    # no-op keep-alive (debug-pod entrypoint ``-- sleep 3600``; changes nothing)
    "sleep", "true", "echo",
    # filesystem inspection
    "ls", "stat", "readlink", "realpath", "file", "readelf", "cat", "head",
    "tail", "wc", "find", "du", "df", "lsblk", "blkid",
    # text filters (read-only stages of a probe pipeline, e.g. ps aux | grep)
    "grep", "egrep", "fgrep", "sort", "uniq", "cut", "tr", "awk",
    # process / resource inspection
    "ps", "top", "free", "uptime", "vmstat", "iostat", "mpstat", "sar",
    "pidof", "pgrep", "lsof", "lsmod",
    # network inspection
    "ss", "netstat", "ping", "ping6", "nslookup", "dig", "host",
    "wget", "curl",
    # host inspection probes reached through a privileged debug pod: hardware /
    # kernel / filesystem / hashing facts that are read-only REGARDLESS of args
    # in this name-only set. Added after task-3a360709 surfaced read-only host
    # probes rejected as escape mutations. Commands with a mutating sibling are
    # deliberately EXCLUDED here and handled per-argument below: date (-s),
    # route (add/del), ethtool (-s/-K), swapon (bare = enable), conntrack (-D),
    # arp (-d/-s), numactl (runs a wrapped command).
    "findmnt", "mountpoint", "lsns",
    "lscpu", "lspci", "getcap", "getenforce", "sestatus",
    "md5sum", "sha1sum", "sha256sum", "sha512sum", "cksum",
    "base64", "strings", "hexdump", "xxd", "od", "nm", "ldd", "objdump",
})
# Binaries that ARE the injection in an exec context even though their names
# are not fault verbs: load generators, device-mapper, port-occupying
# listeners. Their presence alone marks a mutation.
_MUTATING_BINARIES = frozenset({
    "stress", "stress-ng", "dd", "fallocate", "fio", "dmsetup",
    "nc", "ncat", "socat",
})
# Container-escape primitives reach the host. In a BARE host command they are
# always treated as a mutation (see ``_classify_argv``): ``is_readonly_argv``
# feeds ``host_inject``'s ``skip_guard``, and neither primitive is in
# ToolGuard's allow-list, so admitting them there would open a guard bypass on
# a path that never needs them. Inside a ``kubectl exec`` they ARE the standard
# way to inspect a node from a privileged debug pod (``chroot /host cat
# /etc/os-release``), so ``_classify_inner`` unwraps them and judges the REAL
# command instead — see ``_unwrap_escape``.
_ESCAPE_PRIMITIVES = ("chroot", "nsenter", "unshare")
# Flags that consume a SEPARATE value token, so the parser must skip two tokens.
# Getting these wrong shifts the parser's idea of where the real command starts.
_CHROOT_VALUE_FLAGS = frozenset({"--userspec", "--groups"})
_NSENTER_VALUE_FLAGS = frozenset({
    "-t", "--target", "-S", "--setuid", "-G", "--setgid",
    "-r", "--root", "-w", "--wd", "--wdns",
})


def _unwrap_escape(tokens: list[str]) -> list[str] | None:
    """Strip a leading escape primitive, returning the command it would run.

    Returns ``None`` when the prefix cannot be parsed with confidence — the
    caller must then fail closed rather than guess.

    Forms handled:
      ``chroot /host CMD...``                  → ``CMD...``
      ``chroot --skip-chdir /host CMD...``     → ``CMD...``
      ``nsenter -t 1 -m -n -- CMD...``         → ``CMD...``
      ``nsenter -t1 -m CMD...``                → ``CMD...``
      ``unshare -m CMD...``                    → ``CMD...``
    """
    if not tokens:
        return None
    binary = tokens[0].rsplit("/", 1)[-1]
    rest = tokens[1:]
    if binary == "chroot":
        # chroot [OPTION]... NEWROOT [COMMAND]... — options may PRECEDE NEWROOT
        # (--userspec / --groups / --skip-chdir). Skipping them is mandatory:
        # blindly treating rest[0] as NEWROOT once let
        # ``chroot --skip-chdir /host iptables -F`` through, because the
        # leftover ``/host`` basename collides with the read-only DNS ``host``
        # binary and the real command was never inspected.
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            if rest[i] in _CHROOT_VALUE_FLAGS:
                i += 2  # flag with a separate value
            else:
                i += 1
        # rest[i] is NEWROOT; the command follows it.
        if i + 1 >= len(rest):
            return None
        return rest[i + 1:]
    # nsenter / unshare: an explicit ``--`` separates flags from the command;
    # without it, the command starts at the first token that is neither a flag
    # nor a value consumed by a flag taking an argument.
    if "--" in rest:
        inner = rest[rest.index("--") + 1:]
        return inner or None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if not tok.startswith("-"):
            return rest[i:]
        # "-t 1" (separate value) vs "-t1" / "--target=1" (attached value)
        if tok in _NSENTER_VALUE_FLAGS:
            i += 2
            continue
        i += 1
    return None

# Dual-use arg guards ------------------------------------------------------
_IPTABLES_READONLY_FIRST = (
    "-L", "-S", "--list", "--list-rules", "--version", "-V",
    "--help", "-h", "version",
)
_NFT_READONLY_FIRST = ("list", "--version", "-v", "--help", "-h")
_TC_MUTATING = ("add", "del", "delete", "change", "replace", "mod")
# ChaosBlade CLI — read-only only for its experiment-inspection verbs.
# ``create`` starts an experiment, ``destroy`` ends one (both mutate),
# ``prepare``/``revoke`` install/remove the injection agent. ``status`` /
# ``query`` inspect an experiment UID — the standard post-injection probe
# inside a tool-pod exec. Task-5193538b: ``blade status --uid ...`` was
# recorded as a kubectl-native INJECTION because ``blade`` was in neither
# vocabulary and the fail-safe below judged it mutating.
_BLADE_READONLY_VERBS = frozenset({"status", "query", "version", "-h", "--help"})
# ``ip`` mutating verbs across objects (link/addr/route/neigh/rule/...).
# ``exec`` (``ip netns exec ns1 <cmd>``) runs an ARBITRARY command inside a
# namespace; it appears in no read-only ip invocation, so keying on the bare
# token has zero false positives.
_IP_MUTATING = frozenset({
    "set", "add", "del", "delete", "change", "replace", "flush", "append",
    "exec",
})
_SYSTEMCTL_READONLY_VERBS = frozenset({
    "status", "is-active", "is-enabled", "is-failed",
    "show", "list-units", "list-unit-files",
})
_MOUNT_MUTATING_FLAGS = ("-o", "--options", "--bind", "--move", "-B", "-M",
                         "--rbind", "--make-shared", "--remount",
                         # ``mount -a`` mounts everything in fstab — a mutation
                         # even with no positional target.
                         "-a", "--all")
# ``-a`` also bundles (``mount -av``), so the cluster is scanned too.
_MOUNT_MUTATING_SHORT_CHARS = frozenset("a")
_MOUNT_VALUELESS_SHORT = frozenset("avrwnfli")
_DMESG_MUTATING_FLAGS = ("-C", "--clear", "-c", "--read-clear")
# ``-C``/``-c`` bundle with the display flags (``dmesg -cT`` clears AND prints).
_DMESG_MUTATING_SHORT_CHARS = frozenset("Cc")
_DMESG_VALUELESS_SHORT = frozenset("CcTxkurtHwdePS")
# journalctl reads the journal; only its maintenance verbs write to it.
_JOURNALCTL_MUTATING_FLAGS = (
    "--rotate", "--flush", "--sync", "--relinquish-var",
    "--vacuum-size", "--vacuum-time", "--vacuum-files",
)
# sysctl reads unless a write form is present: ``-w``, ``key=value``, loading
# from a file (``-p``), or applying every config file (``--system``).
_SYSCTL_MUTATING_FLAGS = ("-w", "--write", "-p", "--load", "--system")
# date reads the clock unless it SETS it: ``-s``/``--set`` change the system
# time — which in this project is itself a fault (clock skew), never a probe.
# ``date -s`` appears verbatim in the time-drift skill, so misjudging it as
# read-only would wave an injection through the verify/intent screeners.
_DATE_MUTATING_FLAGS = ("-s", "--set")
# route prints/-n unless it edits the table (``add``/``del``/``delete``/
# ``flush``) — the mutating verbs are positionals, not flags.
_ROUTE_MUTATING_VERBS = frozenset({"add", "del", "delete", "flush", "change"})
# ethtool inspects (bare / ``-i``/``-S``/``-k``/``-g``/``-a``/``-c``) unless a
# CHANGE flag is present. The change flags are the upper-case-ish setters.
_ETHTOOL_MUTATING_FLAGS = frozenset({
    "-s", "--change", "-K", "--features", "--offload",
    "-G", "--set-ring", "-A", "--pause", "-C", "--coalesce",
    "-L", "--set-channels", "-P", "--set-eeprom", "--reset",
})
# conntrack reads with ``-L``/``-S``/``-G``; ``-D``/``-F``/``-U`` delete or
# flush the connection-tracking table (a fault, not an observation).
_CONNTRACK_MUTATING_FLAGS = frozenset({
    "-D", "--delete", "-F", "--flush", "-U", "--update", "-I", "--create",
})
# swapon ENABLES swap by default (a mutation); only ``-s``/``--show``/
# ``--summary`` are the read-only listing form. ``swapoff`` is never read-only.
_SWAPON_READONLY_FLAGS = frozenset({"-s", "--show", "--summary"})
# arp prints the cache unless ``-d`` (delete entry) / ``-s`` (add static) edit it.
_ARP_MUTATING_FLAGS = frozenset({"-d", "--delete", "-s", "--set"})
# find — read-only only WITHOUT its action primitives. ``-exec``/``-ok`` run an
# arbitrary command per match (the ``+`` terminator needs no shell metachar,
# so the string-level screens cannot see it), ``-delete`` removes whole trees,
# ``-fprint*``/``-fls`` write result files.
_FIND_MUTATING_FLAGS = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fls", "-fprint", "-fprintf",
})
# awk is a programming language, not a text filter: ``system(...)`` runs an
# arbitrary command, and ``-f``/``-i``/``@load`` execute program FILES. Its
# in-program redirect (``print > file``) and command pipes contain ``>``/``|``
# and are already caught by the string-level metachar screens every call
# surface applies (host_read's raw-string check, host_inject's read-only
# branch, and the exec-inner control-op scan), so they are not re-checked at
# argv level here.
_AWK_MUTATING_FLAGS = frozenset({"-f", "--file", "-i", "--include"})
# Short forms take an ATTACHED value too (``-f/tmp/prog.awk``), which an
# exact-match check would wave through.
_AWK_MUTATING_SHORT_PREFIXES = ("-f", "-i")
_AWK_MUTATING_RE = re.compile(r"system\s*\(|@load")
# Short options that take NO value, per binary. Needed to read a bundled
# cluster correctly: an option that TAKES a value swallows the rest of the
# token as that value, so ``-XGET`` is "method GET", not "flags X/G/E/T".
# Scanning a cluster past such an option produces false positives (``-XGET``
# contains 'T'), so ``_reachable_cluster`` stops there — see that helper.
# Under-listing costs a missed bundle; over-listing costs a false rejection,
# so only high-confidence valueless flags are listed.
_CURL_VALUELESS_SHORT = frozenset("sSILkvfigGNjpZqnBRJM46hV0123#")
_WGET_VALUELESS_SHORT = frozenset("qvdbcNSkKmrpxEHn46hV")
# curl — read-only only when the response stays on STDOUT (its default). The
# long options below write local files (``--output*``/``--cookie-jar``/
# ``--dump-header``/``--trace*``/``--remote-name``) or move data off-box
# (``--data*``/``--form*``/``--upload*``/``--config``).
_CURL_MUTATING_LONG_PREFIXES = (
    "--output", "--remote-name", "--data", "--form", "--upload", "--config",
    "--cookie-jar", "--dump-header", "--trace",
)
_CURL_MUTATING_SHORT_CHARS = frozenset("oOdFTKcD")
#: Discard sinks. ``-o /dev/null`` (curl) and ``-O /dev/null`` (wget) throw the
#: body away rather than writing a file, which is how a latency probe asks for
#: timing without the payload: ``curl -s -o /dev/null -w '%{time_total}'``. Both
#: forms were refused as "writes local files", so a drill measuring injected
#: network delay had no way to read the actual millisecond figure and fell back
#: to a coarse timeout flip (observed on task-15543b7b, which tried ``-o
#: /dev/null`` and ``-O /dev/null`` in succession and got neither).
#:
#: Only these exact paths. A discard sink is recognised by its path, so anything
#: else — including ``/dev/stdout`` or a writable device — stays refused.
_DISCARD_SINKS = frozenset({"/dev/null"})
#: dd operands that keep a discard-sink read from being read-only. ``seek``
#: positions the OUTPUT, which is meaningless for /dev/null but signals intent
#: to write at an offset; ``conv`` / ``oflag`` change how the sink is opened
#: (``conv=notrunc``, ``oflag=append``) and ``status`` is the only other operand
#: worth allowing. Anything that is not a pure read parameter is refused so a
#: discard sink cannot be used to smuggle a write form past the check.
_DD_MUTATING_OPERANDS = ("seek", "conv", "oflag")

# wget — its DEFAULT is to write the response into a cwd file, so the verdict
# is inverted: read-only only for ``--spider`` or output explicitly redirected
# to stdout (``-O -`` / ``--output-document=-`` / bundled ``-qO-``).
# ``-o``/``-a``/``--output-file`` redirect the LOG to a file; ``--post-*``/
# ``--body-file``/``--upload-file`` transmit data off-box. Both matter even
# with ``--spider``, which is why they are checked before it.
_WGET_MUTATING_LONG_PREFIXES = (
    "--post", "--body-file", "--upload-file", "--output-file",
)
_WGET_MUTATING_SHORT = frozenset({"-o", "-a"})
# The remaining table entries that can execute a command or write a file. Same
# root cause as find/awk/curl/wget: a name that reads as "diagnostic" while the
# argument list decides.
#
# ``sort -o`` / ``sar -o`` write (and TRUNCATE) an arbitrary path; ``ss -K``
# forcibly closes matching sockets — that IS a fault injection; ``uniq``'s
# SECOND positional is an output file, so its value-taking flags must be
# skipped before positionals are counted (``uniq -f 2 in`` has one, not two).
# ``command`` is handled separately: ``command -v X`` only resolves a path,
# while ``command X args`` RUNS X.
#
# Short forms are PREFIXES throughout: GNU getopt accepts an attached value
# (``sort -o/etc/passwd``), which an exact-token check would wave through.
_SORT_MUTATING_SHORT_PREFIXES = ("-o",)
_SORT_MUTATING_LONG_FLAGS = frozenset({"--output"})
_SAR_MUTATING_SHORT_PREFIXES = ("-o",)
_SS_MUTATING_FLAGS = frozenset({"-K", "--kill"})
# ``-f``/``-N`` are omitted deliberately: they TAKE a value (family / netns), so
# listing them would let the cluster scan run into that value and reject a
# namespace named e.g. "K8s".
_SS_VALUELESS_SHORT = frozenset("tuwxnlapemios46rZzdgHSbEM")
_UNIQ_VALUE_FLAGS = frozenset({
    "-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars",
})
# Container-runtime CLIs are dual-use. Only LEAF inspection verbs are read-only;
# ``exec`` is deliberately excluded because its inner command is unbounded, and
# lifecycle verbs (rm/kill/stop/...) mutate workloads.
#
# Grouping verbs (``image`` / ``config`` / ``container`` / ``volume`` / ``network``)
# are deliberately ABSENT: they take a mutating sub-verb, and a verdict based on
# the first token alone would admit ``docker image rm X`` / ``crictl config
# --set`` as "read-only". Listing forms have their own leaf verbs (``images``,
# ``ps``), so nothing legitimate is lost.
_RUNTIME_CLIS = ("crictl", "docker", "nerdctl", "podman", "ctr")
_RUNTIME_READONLY_VERBS = frozenset({
    "ps", "images", "inspect", "inspecti", "inspectp",
    "logs", "stats", "statsp", "version", "info", "pods", "top",
    "imagefsinfo", "port", "events",
})
# Runtime-CLI global flags that consume a separate value; their value must not
# be mistaken for the verb (e.g. ``crictl --runtime-endpoint unix://... ps``).
_RUNTIME_VALUE_FLAGS = frozenset({
    "-r", "--runtime-endpoint", "-i", "--image-endpoint",
    "-t", "--timeout", "-c", "--config", "-H", "--host",
    "--context", "--log-level", "-n", "--namespace", "--address",
    "--tlscacert", "--tlscert", "--tlskey", "-D", "--debug-dir",
})
# Wrappers that prefix a real command; the wrapped command decides the verdict.
_COMMAND_WRAPPERS = ("timeout", "stdbuf", "nice", "ionice", "env")
# Wrapper flags consuming a separate value — skipping only the flag would leave
# its value to be mistaken for the wrapped command.
_WRAPPER_VALUE_FLAGS = frozenset({
    "-n", "-c", "-p", "-o", "-i", "-e", "-k", "-s", "-u",
    "--kill-after", "--signal", "--unset", "--chdir",
    "--class", "--classdata", "--pid", "--output", "--input", "--error",
})
# ``timeout``'s DURATION positional: a number with an optional unit suffix.
_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")

# Shell control operators that make an inner command a compound script rather
# than a single probe — fail closed to non-read-only. ``|`` is handled
# separately (pipeline of read-only stages is allowed for exec probes).
_SHELL_CONTROL_OPS = (">", "<", "`", "$(", "&&", "||", ";", "&", "\n")
# Metacharacters rejected outright for a bare host command. A host channel
# hands the command to a remote shell (``ssh -- host "<str>"`` /
# ``wiz task exec --command "<str>"``), but ``wrap_command`` ``shlex.quote``s
# EVERY token first, so a ``|`` arrives as the quoted literal ``'|'`` — a
# non-functional argument rather than a pipeline operator. Rejecting these
# up front turns a silently useless command into an actionable error.
# (Because a remote shell IS in the loop, shell BUILTINS such as
# ``command -v`` do work — see the capability-probe guidance in host_cmd.)
_HOST_METACHARS = ("|", ">", "<", ";", "&", "`", "$(", "\n")


def _host_entry_tokens(inner: list[str]) -> list[str]:
    """Unwrap a single ``sh -c "<script>"`` layer to reach the real entry."""
    if inner and inner[0] in _SHELL_WRAPPERS and "-c" in inner:
        idx = inner.index("-c")
        if idx + 1 < len(inner):
            try:
                nested = shlex.split(inner[idx + 1])
            except ValueError:
                return inner
            if nested:
                return nested
    return inner


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Strip leading command wrappers (``timeout 5``, ``nice -n 5``, ``env A=1``).

    Returns the wrapped command, or the original tokens when nothing is wrapped
    (so a bare ``env`` still classifies as the environment dump it is).
    """
    depth = 0
    while tokens and depth < 3:
        binary = tokens[0].rsplit("/", 1)[-1]
        if binary not in _COMMAND_WRAPPERS:
            return tokens
        rest = tokens[1:]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in _WRAPPER_VALUE_FLAGS:
                i += 2       # flag consuming a separate value (``nice -n 5``)
            elif tok.startswith("-") or "=" in tok:
                i += 1       # valueless flag, or an ``env VAR=VAL`` assignment
            elif binary == "timeout" and _DURATION_RE.match(tok):
                i += 1       # timeout's DURATION positional
            else:
                break        # first real token of the wrapped command
        rest = rest[i:]
        if not rest:
            return tokens  # nothing wrapped — judge the wrapper itself
        tokens = rest
        depth += 1
    return tokens


def _reachable_cluster(token: str, valueless: frozenset[str]) -> str:
    """Option characters an argv-level check may judge inside a short cluster.

    A short option that takes a value swallows the REST of the token as that
    value, so scanning every character is wrong in both directions:
    ``curl -XGET`` would trip on the 'T' of "GET" (false rejection), while
    ``curl -so /root/x`` must trip on the 'o' (a real write).

    Returns the leading run of known valueless flag characters PLUS the first
    character that is not one — that character is still an option and worth
    judging, but everything after it is its value.
    """
    if not token.startswith("-") or token.startswith("--"):
        return ""
    out: list[str] = []
    for ch in token[1:]:
        out.append(ch)
        if ch not in valueless:
            break  # takes a value: the remainder of the token IS that value
    return "".join(out)


def _drop_discard_output(
    args: list[str], flags: tuple[str, ...], *, cluster_of: frozenset[str]
) -> list[str]:
    """Remove ``<flag> /dev/null`` pairs so the mutating scan does not see them.

    Writing to a discard sink is not a write (see :data:`_DISCARD_SINKS`), but
    the scans that follow judge a token by its flag alone and cannot look at the
    value. Consuming the pair here keeps those scans untouched: every other
    write or upload flag still reaches them, and a non-discard value leaves the
    flag in place so it is refused exactly as before.

    Handles the three spellings a caller may use: separate (``-o /dev/null``),
    attached long (``--output=/dev/null``), and bundled short (``-so
    /dev/null``) — for the bundle only the output character is dropped, the rest
    of the cluster is preserved so a co-bundled write flag is still caught.
    """
    short = tuple(f for f in flags if not f.startswith("--"))
    long_ = tuple(f for f in flags if f.startswith("--"))
    out: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None

        # ``--output=/dev/null``
        if tok.startswith(long_) and "=" in tok:
            name, value = tok.split("=", 1)
            if name in long_ and value in _DISCARD_SINKS:
                i += 1
                continue

        # ``-o /dev/null`` / ``--output /dev/null``
        if tok in flags and nxt in _DISCARD_SINKS:
            i += 2
            continue

        # ``-so /dev/null`` — drop only the output char from the cluster.
        if nxt in _DISCARD_SINKS and tok.startswith("-") and not tok.startswith("--"):
            cluster = _reachable_cluster(tok, cluster_of)
            chars = {f.lstrip("-") for f in short}
            if cluster and cluster[-1] in chars:
                kept = "-" + cluster[:-1]
                if len(kept) > 1:
                    out.append(kept)
                i += 2
                continue

        out.append(tok)
        i += 1
    return out


def _classify_argv(tokens: list[str], _depth: int = 0) -> tuple[bool, str | None]:
    """Classify a single command (one pipeline stage). Returns (ok, reason)."""
    if not tokens:
        return True, None
    binary = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]

    # Wrappers (``timeout 5 <cmd>``, ``nice -n 5 <cmd>``, ``env A=1 <cmd>``):
    # the wrapped command decides the verdict.
    if binary in _COMMAND_WRAPPERS and _depth < 3:
        unwrapped = _strip_wrappers(tokens)
        if unwrapped is not tokens and unwrapped != tokens:
            return _classify_argv(unwrapped, _depth + 1)
        # No wrapped command: ``env`` alone dumps the environment (read-only);
        # a bare wrapper otherwise does nothing observable.
        return (True, None) if binary in _READONLY_BINARIES else (
            False, f"'{binary}' wraps no command, so read-only status cannot be determined"
        )

    if binary in _ESCAPE_PRIMITIVES or binary.startswith("/host"):
        return False, f"'{binary}' reaches the host / escapes the container, not a read-only probe"

    # Netfilter tooling — read-only only in list/version forms.
    if binary in ("iptables", "ip6tables"):
        ok = bool(args) and args[0] in _IPTABLES_READONLY_FIRST
        if ok:
            return True, None
        return False, (
            f"'{binary}' is read-only only with -L/-S/--list/--version "
            f"(got '{args[0] if args else 'no arguments'}'; -A/-D/-I/-F etc. mutate)"
        )
    if binary == "nft":
        ok = bool(args) and args[0] in _NFT_READONLY_FIRST
        if ok:
            return True, None
        return False, "'nft' is read-only only with list/--version (add/delete/flush mutate)"
    if binary == "tc":
        mutating = any(a in _TC_MUTATING for a in args)
        if not mutating:
            return True, None
        return False, "'tc' add/del/change/replace mutate (only show/qdisc queries are read-only)"

    # blade — read-only only for experiment inspection (see _BLADE_READONLY_VERBS).
    if binary == "blade":
        if args and args[0] in _BLADE_READONLY_VERBS:
            return True, None
        return False, (
            "'blade' is read-only only for status/query/version "
            f"(got '{args[0] if args else 'no arguments'}'; create/destroy/prepare/revoke mutate)"
        )

    # ip — read-only unless a mutating verb (set/add/del/...) is present.
    if binary == "ip":
        mutating = any(a in _IP_MUTATING for a in args)
        if not mutating:
            return True, None
        return False, "'ip' is read-only only with show/list/get (set/add/del/flush mutate)"

    # systemctl — read-only only for its status/show verbs.
    if binary == "systemctl":
        verb = next((a for a in args if not a.startswith("-")), "")
        if verb in _SYSTEMCTL_READONLY_VERBS:
            return True, None
        return False, (
            "'systemctl' is read-only only for verbs like status/is-active/is-enabled/show/list-units "
            f"(got '{verb or 'no verb'}'; start/stop/restart mutate)"
        )

    # mount — read-only only when listing (no target device/dir, no remount).
    if binary == "mount":
        positionals = [a for a in args if not a.startswith("-")]
        mutating = bool(positionals) or any(
            a in _MOUNT_MUTATING_FLAGS
            or a.startswith("-o")
            or any(ch in _MOUNT_MUTATING_SHORT_CHARS
                   for ch in _reachable_cluster(a, _MOUNT_VALUELESS_SHORT))
            for a in args
        )
        if not mutating:
            return True, None
        return False, "'mount' is read-only only with no arguments or -l (mounting/-o/-a/remount mutate)"

    # dmesg — read-only unless clearing the ring buffer.
    if binary == "dmesg":
        if any(
            a in _DMESG_MUTATING_FLAGS
            or any(ch in _DMESG_MUTATING_SHORT_CHARS
                   for ch in _reachable_cluster(a, _DMESG_VALUELESS_SHORT))
            for a in args
        ):
            return False, "'dmesg' -C/-c clears the kernel ring buffer, which mutates (read-only only reads)"
        return True, None

    # journalctl — reading the journal is read-only; maintenance verbs are not.
    if binary == "journalctl":
        bad = next(
            (a for a in args
             if a.split("=")[0] in _JOURNALCTL_MUTATING_FLAGS), None,
        )
        if bad:
            return False, (
                f"'journalctl' {bad} writes to / cleans up the journal store, which mutates"
                " (read-only only queries, e.g. -u/-n/--since)"
            )
        return True, None

    # sysctl — reading a key is read-only; -w / key=value / -p write kernel params.
    if binary == "sysctl":
        if any(a in _SYSCTL_MUTATING_FLAGS for a in args) or any(
            "=" in a and not a.startswith("-") for a in args
        ):
            return False, (
                "'sysctl' is read-only only in read form (e.g. -a / -n key / key)"
                "; -w, key=value and -p write kernel parameters"
            )
        return True, None

    # date — reading the clock is a probe; ``-s``/``--set`` IS the clock-skew
    # fault. ``--set=...`` is caught by prefix so the value cannot hide it.
    if binary == "date":
        bad = next(
            (a for a in args
             if a in _DATE_MUTATING_FLAGS or a.startswith("--set=")), None,
        )
        if bad is not None:
            return False, f"'date' {bad} sets the system clock, which mutates (that IS the clock-skew fault, not a probe)"
        return True, None

    # route — printing the table is read-only; add/del/flush edit it.
    if binary == "route":
        bad = next((a for a in args if a in _ROUTE_MUTATING_VERBS), None)
        if bad is not None:
            return False, f"'route' {bad} edits the routing table, which mutates (only no arguments or -n listing is read-only)"
        return True, None

    # ethtool — inspects by default; setter flags change the NIC.
    if binary == "ethtool":
        bad = next((a for a in args if a in _ETHTOOL_MUTATING_FLAGS), None)
        if bad is not None:
            return False, (
                f"'ethtool' {bad} changes the NIC configuration, which mutates"
                " (only no arguments or -i/-S/-k/-g/-a/-c queries are read-only)"
            )
        return True, None

    # conntrack — -L/-S/-G read; -D/-F/-U/-I delete or flush the table.
    if binary == "conntrack":
        bad = next((a for a in args if a in _CONNTRACK_MUTATING_FLAGS), None)
        if bad is not None:
            return False, (
                f"'conntrack' {bad} deletes / flushes the connection-tracking table, which mutates (only -L/-S/-G are read-only)"
            )
        return True, None

    # swapon — ENABLES swap by default; only the listing flags are read-only.
    if binary == "swapon":
        if any(a in _SWAPON_READONLY_FLAGS for a in args):
            return True, None
        return False, "'swapon' enables swap by default, which mutates (only the -s/--show listing is read-only)"

    # arp — prints the cache unless -d (delete) / -s (add static) edit it.
    if binary == "arp":
        bad = next((a for a in args if a in _ARP_MUTATING_FLAGS), None)
        if bad is not None:
            return False, f"'arp' {bad} edits the ARP cache, which mutates (only no arguments or -a/-n queries are read-only)"
        return True, None

    # numactl — ``-H``/``--hardware`` / ``-s``/``--show`` inspect; any other form
    # RUNS a wrapped command (``numactl --physcpubind=0 stress ...``), so the
    # wrapped command must decide. Delegate exactly like the env/timeout path.
    if binary == "numactl":
        _RO = {"-H", "--hardware", "-s", "--show"}
        non_opt = [a for a in args if not a.startswith("-")]
        if not non_opt:
            # No wrapped command: read-only only if every flag is an inspect flag.
            if args and all(a in _RO for a in args):
                return True, None
            return False, "'numactl' is read-only only for -H/--hardware/-s/--show queries"
        if _depth >= 3:
            return False, "'numactl' nesting is too deep to determine read-only status reliably"
        # First non-option token onward is the wrapped command it runs.
        return _classify_argv(args[args.index(non_opt[0]):], _depth + 1)

    # Container-runtime CLIs — only leaf inspection verbs (ps/inspect/logs/...).
    if binary in _RUNTIME_CLIS:
        verb = ""
        i = 0
        while i < len(args):
            tok = args[i]
            if not tok.startswith("-"):
                verb = tok
                break
            i += 2 if tok in _RUNTIME_VALUE_FLAGS else 1
        if verb in _RUNTIME_READONLY_VERBS:
            return True, None
        return False, (
            f"'{binary}' is read-only only for query verbs like ps/images/inspect/logs/stats/version "
            f"(got '{verb or 'no verb'}'; exec/rm/kill/stop/run, "
            "and grouped verbs with sub-verbs such as image/config, all mutate)"
        )

    # find — read-only only WITHOUT its action primitives. ``-exec``/``-ok``
    # run an arbitrary command per match (the ``+`` terminator carries no
    # shell metacharacter, so the string-level screens cannot see it),
    # ``-delete`` removes whole trees, ``-fprint*``/``-fls`` write files.
    if binary == "find":
        bad = next((a for a in args if a in _FIND_MUTATING_FLAGS), None)
        if bad is not None:
            return False, (
                f"'find' is read-only only for traversal/printing; {bad} runs commands / deletes / writes files"
            )
        return True, None

    # awk — a programming language, not a filter. ``system(...)`` runs an
    # arbitrary command; ``-f``/``-i``/``@load`` execute program FILES. The
    # in-program redirect (``print > file``) and command pipes contain
    # ``>``/``|`` and are caught by the string-level metachar screens applied
    # on every call surface (host_read, host_inject's read-only branch, and
    # the exec-inner control-op scan), so they need no argv-level re-check.
    if binary == "awk":
        bad = next(
            (a for a in args
             if a.split("=", 1)[0] in _AWK_MUTATING_FLAGS
             or a.startswith(_AWK_MUTATING_SHORT_PREFIXES)
             or _AWK_MUTATING_RE.search(a)),
            None,
        )
        if bad is not None:
            return False, (
                f"'awk' is read-only only for filtering/printing; {bad} can run commands or load program files"
                " (system()/@load/-f)"
            )
        return True, None

    # curl — read-only only when the response stays on STDOUT (the default).
    # Output/upload forms write local files or move host data off-box. Short
    # forms are read through ``_reachable_cluster`` so a bundled ``-so file``
    # is caught while a value-carrying ``-XGET`` is not mistaken for flags.
    #
    # ``-o /dev/null`` is the exception: it discards the body instead of writing
    # a file, which is the standard way to time a request without its payload.
    # Recognised before the mutating scan, and only for that flag — an upload or
    # a config read stays refused however its own value is spelled.
    if binary == "curl":
        args = _drop_discard_output(args, ("-o", "--output"), cluster_of=_CURL_VALUELESS_SHORT)
        bad = next(
            (a for a in args
             if a.startswith(_CURL_MUTATING_LONG_PREFIXES)
             or any(ch in _CURL_MUTATING_SHORT_CHARS
                    for ch in _reachable_cluster(a, _CURL_VALUELESS_SHORT))),
            None,
        )
        if bad is not None:
            return False, (
                f"'curl' is read-only only when GET/HEAD output goes to stdout; {bad} writes local files"
                " or uploads data"
            )
        return True, None

    # wget — DEFAULT is to write the response into a cwd file, so the verdict
    # is inverted: read-only only for ``--spider`` or output explicitly sent
    # to stdout (``-O -`` / ``--output-document=-`` / bundled ``-qO-``).
    #
    # ``-o``/``-a``/``--output-file`` redirect the LOG, a sink separate from the
    # document, so they are dropped here on the same discard rule: ``wget -o
    # /dev/null -qO- <url>`` keeps both sinks off disk. The document check below
    # is untouched and still decides on its own.
    if binary == "wget":
        args = _drop_discard_output(
            args, ("-o", "-a", "--output-file"), cluster_of=_WGET_VALUELESS_SHORT
        )
        bad = next(
            (a for a in args
             if a.startswith(_WGET_MUTATING_LONG_PREFIXES)
             or a in _WGET_MUTATING_SHORT),
            None,
        )
        if bad is not None:
            return False, (
                f"'wget' is read-only only with --spider or output to stdout; {bad} writes local files"
                " or uploads data"
            )
        if "--spider" in args:
            return True, None
        stdout_out = False
        for i, a in enumerate(args):
            if a in ("-O", "--output-document"):
                # ``-`` is stdout; ``/dev/null`` discards. Both leave nothing on
                # disk, which is what this check is actually asking about.
                nxt = args[i + 1] if i + 1 < len(args) else None
                stdout_out = nxt == "-" or nxt in _DISCARD_SINKS
            elif a.startswith("--output-document="):
                value = a.split("=", 1)[1]
                stdout_out = value == "-" or value in _DISCARD_SINKS
            else:
                # Bundled cluster (``-O-`` / ``-qO-``): only when ``O`` is the
                # LAST reachable option character is the tail its value. A
                # value-carrying option earlier in the token (``-UMozillaO-``)
                # makes the trailing "O-" part of that value, not an output
                # redirect — treating it as one would call a cwd write
                # read-only.
                cluster = _reachable_cluster(a, _WGET_VALUELESS_SHORT)
                if cluster.endswith("O"):
                    tail = a.split("O", 1)[1]
                    # ``-qO /dev/null`` puts the sink in the NEXT token.
                    if tail == "":
                        nxt = args[i + 1] if i + 1 < len(args) else None
                        stdout_out = nxt == "-" or nxt in _DISCARD_SINKS
                    else:
                        stdout_out = tail == "-" or tail in _DISCARD_SINKS
        if stdout_out:
            return True, None
        return False, (
            "'wget' writes the response into a file in the current directory by default; only --spider,"
            " -O- (stdout) or -O /dev/null (discard) is read-only"
        )

    # command — ``command -v X`` resolves a path and runs nothing (the probe
    # form the prompts recommend). Without ``-v``/``-V`` it EXECUTES X, so the
    # wrapped command decides, exactly as for env/timeout/nice.
    if binary == "command":
        if any(a in ("-v", "-V") for a in args):
            return True, None
        # ``command [-p] CMD [ARGS...]``: skip only ``command``'s OWN leading
        # options, then pass the remainder VERBATIM — the same shape
        # ``_strip_wrappers`` uses. Filtering out every dash token instead
        # would hand the wrapped guard an argument list with its own mutating
        # flags removed, turning ``command`` into a bypass prefix for the whole
        # set (``command sort -o /etc/cron.d/evil in`` → "read-only").
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 1
        wrapped = args[i:]
        if not wrapped:
            return True, None  # bare ``command`` does nothing observable
        if _depth >= 3:
            return False, "'command' nesting is too deep to determine read-only status reliably"
        ok, reason = _classify_argv(wrapped, _depth + 1)
        if ok:
            return True, None
        return False, f"'command' does not execute a read-only command: {reason}"

    # sort / sar — ``-o`` writes (and truncates) an arbitrary path. The short
    # form is matched as a PREFIX because GNU getopt accepts an attached value
    # (``sort -o/etc/cron.d/evil``); an exact-token check waves that through.
    if binary == "sort":
        bad = next(
            (a for a in args
             if a.startswith(_SORT_MUTATING_SHORT_PREFIXES)
             or a.split("=", 1)[0] in _SORT_MUTATING_LONG_FLAGS),
            None,
        )
        if bad is not None:
            return False, f"'sort' {bad} writes (and truncates) a file, not a read-only diagnostic"
        return True, None
    if binary == "sar":
        bad = next(
            (a for a in args if a.startswith(_SAR_MUTATING_SHORT_PREFIXES)), None,
        )
        if bad is not None:
            return False, f"'sar' {bad} writes a data file, not a read-only diagnostic"
        return True, None

    # ss — ``-K``/``--kill`` force-closes every matching socket. That is a
    # fault injection, not an observation.
    if binary == "ss":
        bad = next(
            (a for a in args
             if a in _SS_MUTATING_FLAGS
             or "K" in _reachable_cluster(a, _SS_VALUELESS_SHORT)),
            None,
        )
        if bad is not None:
            return False, f"'ss' {bad} force-closes every matching socket, which is fault injection"
        return True, None

    # uniq — ``uniq INPUT OUTPUT``: the second positional is an output file.
    if binary == "uniq":
        positionals: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a in _UNIQ_VALUE_FLAGS:
                i += 2  # flag consuming a separate value (``-f 2``)
                continue
            if not a.startswith("-"):
                positionals.append(a)
            i += 1
        if len(positionals) > 1:
            return False, (
                f"'uniq' treats the second positional argument as an output file ({positionals[1]}), which writes to a file"
            )
        return True, None

    # dd — a writer by default, and that is why it sits in
    # ``_MUTATING_BINARIES``. But ``of=/dev/null`` makes it a pure reader, and
    # that form is how disk-IO verification measures read throughput: the skill
    # cases themselves run ``dd if=<file> of=/dev/null bs=1M count=100`` to show
    # a latency injection slowed reads down. Refusing it left no standard way to
    # time a read at all.
    #
    # Requires an explicit discard ``of=`` AND a source ``if=``. Without ``if=``
    # dd reads stdin, which no probe surface supplies — the form is either a
    # no-op or half of a pipeline, so it earns no exemption. Bare ``dd`` and any
    # real output path stay refused, as do the conversion operands that change
    # what is written even when the sink is discarded.
    if binary == "dd":
        operands = {
            k: v for k, _, v in (a.partition("=") for a in args) if _
        }
        if operands.get("of") in _DISCARD_SINKS and operands.get("if"):
            bad = next((f for f in _DD_MUTATING_OPERANDS if f in operands), None)
            if bad is None:
                return True, None
            return False, (
                f"'dd' reading into a discard sink is read-only, but {bad}= changes what is written"
            )

    if binary in _MUTATING_BINARIES:
        return False, f"'{binary}' is a write/load-generating command, not a read-only diagnostic"
    if binary in _READONLY_BINARIES:
        return True, None
    return False, f"'{binary}' is not a known read-only diagnostic command"


def _classify_inner(inner: list[str], _depth: int = 0) -> tuple[bool, str | None]:
    """Classify a kubectl-exec inner command (after ``--``).

    Unwraps one ``sh -c`` layer, fails closed on any shell control operator,
    and requires every pipeline stage to be a read-only probe.

    Escape primitives are unwrapped here (unlike in a bare host command): from a
    privileged debug pod, ``chroot /host <cmd>`` / ``nsenter -t 1 -m -- <cmd>``
    is the ONLY way to inspect the node, and Phase 1 must be able to verify host
    preconditions (does the node have iptables/systemd?) before committing a
    plan. The verdict is decided by the command actually being run, so
    ``chroot /host iptables -A ...`` stays non-read-only.
    """
    if not inner:
        return True, None  # bare exec (interactive/attach) → read-only
    inner = _host_entry_tokens(inner)
    if not inner:
        return False, "sh -c body is empty or cannot be parsed"

    # Escape prefix: judge the wrapped command. Wrappers are stripped first so
    # ``timeout 5 chroot /host df -h`` is recognised as an escape probe rather
    # than falling through to the bare-argv path (which rejects all escapes).
    # Depth-capped so a nested ``chroot /host chroot /host ...`` cannot spin, and
    # fail-closed whenever the prefix cannot be parsed with confidence.
    inner = _strip_wrappers(inner)
    entry = inner[0].rsplit("/", 1)[-1] if inner else ""
    if entry in _ESCAPE_PRIMITIVES:
        if _depth >= 2:
            return False, f"'{entry}' nesting is too deep to determine read-only status reliably"
        unwrapped = _unwrap_escape(inner)
        if not unwrapped:
            return False, (
                f"'{entry}' is followed by no parseable command, so it is treated as unsafe"
                " (a read-only probe must look like: chroot /host <read-only command>)"
            )
        ok, reason = _classify_inner(unwrapped, _depth + 1)
        if ok:
            return True, None
        return False, f"'{entry}' does not run a read-only command once on the host: {reason}"

    inner_str = " ".join(inner)
    for op in _SHELL_CONTROL_OPS:
        if op in inner_str:
            return False, (
                f"contains the shell control operator '{op.strip() or op!r}'"
                " (redirect/command chain/background/substitution), which a read-only probe does not allow"
            )
    if "|" in inner:
        stages: list[list[str]] = []
        current: list[str] = []
        for tok in inner:
            if tok == "|":
                stages.append(current)
                current = []
            else:
                current.append(tok)
        stages.append(current)
        for stage in stages:
            if not stage:
                continue
            ok, reason = _classify_argv(stage)
            if not ok:
                return False, f"a pipeline stage is not read-only: {reason}"
        return True, None
    return _classify_argv(inner)


def _parse_exec_inner(v_args: str) -> tuple[bool, list[str] | None, str | None]:
    """Split ``POD [-n NS] [-c C] -- INNER`` into its inner tokens.

    Returns ``(has_inner, inner_tokens, parse_error)``. ``has_inner`` is False
    for a pure entry (no ``--``) which is always read-only.
    """
    try:
        tokens = shlex.split(v_args)
    except ValueError:
        return True, None, "command cannot be parsed (unbalanced shell quotes)"
    if "--" not in tokens:
        return False, None, None  # no inner command (pure entry) → read-only
    inner = tokens[tokens.index("--") + 1:]
    return True, inner, None


# --- Public API: bool views + reason views (single source of truth) --------

def is_readonly_argv(argv: list[str]) -> bool:
    """True if a single command (one pipeline stage) is a read-only probe."""
    return _classify_argv(argv)[0]


def is_readonly_inner_tokens(inner: list[str]) -> bool:
    """True if a kubectl-exec inner command (tokens after ``--``) is read-only."""
    return _classify_inner(inner)[0]


def is_readonly_kubectl_exec(v_args: str) -> bool:
    """True if a ``kubectl exec``/``debug`` inner command is a read-only probe.

    Parses ``POD [-n NS] [-c C] -- INNER``, unwraps one ``sh -c`` layer, and
    treats the command as read-only only when every pipeline stage is a known
    inspection command. Any shell control operator fails closed to mutating; a
    bare exec with no inner command is read-only.
    """
    has_inner, inner, parse_error = _parse_exec_inner(v_args)
    if parse_error is not None:
        return False
    if not has_inner:
        return True
    return _classify_inner(inner or [])[0]


def kubectl_exec_rejection_reason(v_args: str) -> str | None:
    """Specific reason a ``kubectl exec``/``debug`` inner command is NOT
    read-only, or ``None`` when it IS read-only."""
    has_inner, inner, parse_error = _parse_exec_inner(v_args)
    if parse_error is not None:
        return parse_error
    if not has_inner:
        return None
    ok, reason = _classify_inner(inner or [])
    return None if ok else reason


def is_readonly_host_command(command: str) -> bool:
    """True if a bare host command is a single read-only diagnostic.

    No shell metacharacters (pipe/redirect/chain/substitution): a host channel
    does reach a remote shell, but ``wrap_command`` quotes every token, so an
    operator arrives as a literal argument and would silently do nothing.
    """
    return host_command_rejection_reason(command) is None


def contains_shell_metachar(command: str) -> bool:
    """True if a raw command string carries a shell metacharacter.

    The same screen ``host_command_rejection_reason`` applies, exported so
    other read-only fast paths that judge at ARGV level (``host_inject``'s
    ``skip_guard`` branch) can apply it too: the argv classifier alone cannot
    see writes hidden inside a program string (``awk '{print > "/tmp/x"}'``).
    Quoting makes a metachar a useless literal on the wire anyway, so refusing
    loses nothing.
    """
    return any(bad in command for bad in _HOST_METACHARS)


def host_command_rejection_reason(command: str) -> str | None:
    """Specific reason a bare host command is NOT an allowed read-only
    diagnostic, or ``None`` when it is."""
    if not command or not command.strip():
        return "empty command"
    for bad in _HOST_METACHARS:
        if bad in command:
            return (
                f"contains the shell metacharacter '{bad}'"
                " (host_read only runs a single read-only diagnostic with no pipe/redirect)"
            )
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "command cannot be parsed (unbalanced shell quotes)"
    if not tokens:
        return "empty command"
    ok, reason = _classify_argv(tokens)
    return None if ok else reason


__all__ = [
    "is_readonly_argv",
    "is_readonly_inner_tokens",
    "is_readonly_kubectl_exec",
    "kubectl_exec_rejection_reason",
    "is_readonly_host_command",
    "contains_shell_metachar",
    "host_command_rejection_reason",
]
