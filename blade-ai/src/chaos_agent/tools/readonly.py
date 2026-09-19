"""Shared read-only command classifier — the single source of truth for
"is this shell command a read-only probe?".

Three separate judgments used to answer this question with divergent
vocabularies:

  - ``k8s_native._is_readonly_exec_probe`` — ``detect()`` injection attribution.
  - ``providers.k8s_native.classifier._classify_kubectl_exec`` — the guard SCOPE for
    the screeners (moved out of ``target_guard/classifier.py`` in phase-7 T5).
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
    single read-only diagnostic with NO UNQUOTED shell operators (pipe /
    redirect / chain / substitution — quoted literals like ``'a|b'`` are
    fine), matching the former host-profile
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

# The raw-string public surfaces (``host_command_rejection_reason`` /
# ``contains_shell_metachar`` / ``kubectl_exec_rejection_reason`` /
# ``is_readonly_argv``, plus their boolean views) run on the bashfacts
# structural judge in ``_readonly_facts`` — the ONLY engine since the
# engine flip deleted the legacy substring/shlex chain. The argv-level
# classifiers below (``_classify_argv`` / ``_classify_inner``) are NOT
# legacy residue: the facts judge reuses them for argv-semantics policy
# (binary/flag matching — see design 4.2 fact/policy split), and
# ``is_readonly_inner_tokens`` remains the token fallback for callers
# with no raw command text (the classifier's synthetic arg shapes).


def _facts_engine():
    """Lazy-import the facts engine (it imports THIS module's
    ``_classify_argv``, so a top-level import here would be circular)."""
    from chaos_agent.tools import _readonly_facts

    return _readonly_facts


# 4.5 fail-closed matrix: an internal error inside the facts engine must be
# reported TRUTHFULLY (never disguised as the command's syntax problem) and
# always fails closed. No fix-path suggestion pairs with it — the guard's
# own problem has no user-side fix.
_INTERNAL_ERROR_REASON = (
    "guard parser internal error; refusing fail-closed "
    "— very likely not your command's problem"
)


def _facts_verdict(thunk, *, on_error):
    """Run a facts-engine verdict behind the 4.5 internal-error net."""
    try:
        return thunk()
    except Exception:  # the guard must never crash its caller
        return on_error


# One ``sh -c "<script>"`` wrapper is peeled to reach the real entry token
# (mirrors ``target_guard.carriers._host_entry_tokens``; replicated here so this
# module stays in the ``tools`` layer with no agent-package import). Nested
# shells beyond one layer are unusual for a probe and keep failing closed.
_SHELL_WRAPPERS = ("sh", "bash", "ash", "dash", "/bin/sh", "/bin/bash")

# Read-only diagnostics — the TAIL allowlist of ``_classify_argv`` (union
# of the k8s exec-probe vocabulary and the host baseline diagnostic
# whitelist). Dual-use probe binaries (curl/wget/find/awk/ss...) ARE listed
# here, but the argument-level guards in ``_classify_argv`` run FIRST —
# only their guard-cleared shapes reach this allow pass. Binaries with a
# mutating sibling and no probe vocabulary are deliberately EXCLUDED
# instead and allowed per-argument below (see the host-probe exclusion note).
_READONLY_BINARIES = frozenset(
    {
        # identity / capability inspection
        "which",
        "type",
        "command",
        "test",
        "[",
        "uname",
        "id",
        "hostname",
        "whoami",
        "getent",
        "env",
        "printenv",
        "nproc",
        # no-op keep-alive (debug-pod entrypoint ``-- sleep 3600``; changes nothing)
        "sleep",
        "true",
        "echo",
        # filesystem inspection
        # ``cd`` is a shell builtin that changes only the SHELL's own cwd —
        # no system state (``cd /var/log && du -sh .`` is a standard probe
        # and was rejected as "not a known read-only diagnostic command").
        "cd",
        "ls",
        "stat",
        "readlink",
        "realpath",
        "file",
        "readelf",
        "cat",
        "head",
        "tail",
        "wc",
        "find",
        "du",
        "df",
        "lsblk",
        "blkid",
        # text filters (read-only stages of a probe pipeline, e.g. ps aux | grep)
        "grep",
        "egrep",
        "fgrep",
        "sort",
        "uniq",
        "cut",
        "tr",
        "awk",
        # process / resource inspection
        "ps",
        "top",
        "free",
        "uptime",
        "vmstat",
        "iostat",
        "mpstat",
        "sar",
        "pidof",
        "pgrep",
        "lsof",
        "lsmod",
        # network inspection
        "ss",
        "netstat",
        "ping",
        "ping6",
        "nslookup",
        "dig",
        "host",
        "wget",
        "curl",
        # path / reachability probes (send packets, mutate nothing — same class as
        # ``ping``). ``traceroute`` maps hops; ``arping`` resolves a MAC.
        "traceroute",
        "traceroute6",
        "arping",
        # host inspection probes reached through a privileged debug pod: hardware /
        # kernel / filesystem / hashing facts that are read-only REGARDLESS of args
        # in this name-only set. Added after task-3a360709 surfaced read-only host
        # probes rejected as escape mutations. Commands with a mutating sibling are
        # deliberately EXCLUDED here and handled per-argument below: date (-s),
        # route (add/del), ethtool (-s/-K), swapon (bare = enable), conntrack (-D),
        # arp (-d/-s), numactl (runs a wrapped command). Three name-only entries
        # keep an ARGUMENT-level write face judged by their own guards below:
        # dmidecode (--dump-bin), file (-C/--compile), blkid (-g/--garbage-collect).
        "findmnt",
        "mountpoint",
        "lsns",
        "lscpu",
        "lspci",
        "getcap",
        "getenforce",
        "sestatus",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "sha512sum",
        "cksum",
        "base64",
        "strings",
        "hexdump",
        "xxd",
        "od",
        "nm",
        "ldd",
        "objdump",
        # extended session / locale / hardware facts (all dump state, none write):
        # who/w/last enumerate logins, groups/locale/getconf print facts,
        # dmidecode/lshw inspect hardware, whereis locates files.
        # Evidence (strace on al8 host): who/w/last/groups/getconf/whereis/locale/
        # dmidecode/traceroute/arping are fully CLEAN. lshw creates+unlinks a
        # transient probe marker (/var/run/fb-<pid>) that is removed before exit —
        # no residual state. numastat has no binary in the target env; verified by
        # upstream source audit (numactl numastat.c): every fopen is mode "r"
        # (/proc/meminfo, sysfs numastat/meminfo, /proc/<pid>/smaps), the only
        # popen("resize") fires solely when stdout is a TTY — never in exec output.
        "who",
        "w",
        "last",
        "groups",
        "locale",
        "getconf",
        "numastat",
        "dmidecode",
        "lshw",
        "whereis",
    }
)
# Binaries that ARE the injection in an exec context even though their names
# are not fault verbs: load generators, device-mapper, port-occupying
# listeners. Their presence alone marks a mutation.
_MUTATING_BINARIES = frozenset(
    {
        "stress",
        "stress-ng",
        "dd",
        "fallocate",
        "fio",
        "dmsetup",
        "nc",
        "ncat",
        "socat",
    }
)
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
_NSENTER_VALUE_FLAGS = frozenset(
    {
        "-t",
        "--target",
        "-S",
        "--setuid",
        "-G",
        "--setgid",
        "-r",
        "--root",
        "-w",
        "--wd",
        "--wdns",
    }
)


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
        return rest[i + 1 :]
    # nsenter / unshare: an explicit ``--`` separates flags from the command;
    # without it, the command starts at the first token that is neither a flag
    # nor a value consumed by a flag taking an argument.
    if "--" in rest:
        inner = rest[rest.index("--") + 1 :]
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
# The verb may be spelled shortened (``-L``) or long; the long spellings are
# judged by getopt_long's prefix rule (``--lis`` IS ``--list`` — R42), the
# exact short/word forms stay in the first table.
_IPTABLES_READONLY_SHORT = ("-L", "-S", "-V", "-h", "version")
_IPTABLES_READONLY_LONG = ("--list", "--list-rules", "--version", "--help")
# Global options that PRECEDE the command verb and consume a value (``-t nat``)
# or stand alone (``-4``/``-6``/``-w``). The first-token check used to stop at
# these and reject the everyday ``iptables -t nat -L -n`` form; skip them to
# reach the real verb.
_IPTABLES_GLOBAL_VALUE_FLAGS = frozenset({"-t", "--table", "-M", "--modprobe"})
_IPTABLES_GLOBAL_VALUELESS = frozenset({"-4", "-6", "-w", "--wait"})
# Writing command verbs (the command-select cases in xshared.c): -A append,
# -C check, -D delete, -E rename-chain, -F flush, -I insert, -N new-chain,
# -P policy, -R replace, -X delete-chain, -Z zero. Lower-case ``c``
# (--set-counters) rides along: it only rewrites rule counters.
# The verb alone cannot clear a line because a read command COMPOSES with a
# writing one in ONE invocation: add_command(&p->command, CMD_ZERO,
# CMD_LIST | CMD_LIST_RULES) declares the -L/-S + -Z pair LEGAL (xshared.c)
# and ``iptables -L -Z`` really zeroes every chain's counters (R43).
_IPTABLES_WRITE_SHORT_CHARS = frozenset("ACDEFINPRXZc")
_IPTABLES_WRITE_LONG_FLAGS = (
    "--append",
    "--check",
    "--delete",
    "--rename-chain",
    "--flush",
    "--insert",
    "--new-chain",
    "--policy",
    "--replace",
    "--delete-chain",
    "--zero",
)
# Short options that take NO argument at all (OPTSTRING_COMMON spells them
# ``4 6 f n v x``; L/S/X/Z/F are ``::`` optional-arg and A/C/D/E/I/N/P/R take
# a value). A cluster scan may cross only these before stopping: crossing an
# optional-arg option would swallow the next character as that option's VALUE
# (``-LZ`` is the chain named "Z", not -L -Z — verified against
# xshared.h's optstring), so the scan must stop there.
_IPTABLES_VALUELESS_SHORT = frozenset("46fnvx")
# nft's verb is an exact word (nft cmp's the command name); only its two
# option spellings take the prefix rule (``--vers`` — R42).
_NFT_READONLY_SHORT = ("list", "-v", "-h")
_NFT_READONLY_LONG = ("--version", "--help")
# ``tc`` is a TWO-LEVEL command: ``tc [OPTIONS] OBJECT { COMMAND | help }``.
# The object table below is in tc.c's do_cmd dispatch ORDER, because do_cmd
# prefix-matches (``matches(*argv, "qdisc")`` is "the token is a prefix of
# 'qdisc'") and takes the FIRST hit — which is how a bare ``tc e`` reaches the
# ``exec`` object, whose do_exec runs an arbitrary COMMAND. That object makes
# tc a general code runner, so it is refused outright rather than judged.
# The verb vocabulary is diffed against tc(8)'s SYNOPSIS (qdisc/class: add,
# change, replace, delete; qdisc also link; filter/chain: get) — the verbs are
# matched with the same prefix rule, because ``tc q a`` IS ``tc qdisc add``.
# ``mod`` is kept deliberately: it is not in the SYNOPSIS, but deleting a real
# verb would be a fail-open while a surplus entry only refuses a spelling tc
# itself rejects.
_TC_OBJECTS = ("qdisc", "class", "filter", "chain", "actions", "monitor", "exec")
_TC_MUTATING_VERBS = ("add", "change", "replace", "link", "delete", "del", "mod")
# tc's own leading option run consumes the NEXT token for these (main():
# -b/-batch, -n/-netns, -cf/-conf), so they must not be mistaken for the object.
_TC_GLOBAL_VALUE_TOKENS = frozenset({"-b", "-batch", "-n", "-netns", "-cf", "-conf"})
# ChaosBlade CLI — read-only only for its experiment-inspection verbs.
# ``create`` starts an experiment, ``destroy`` ends one (both mutate),
# ``prepare``/``revoke`` install/remove the injection agent. ``status`` /
# ``query`` inspect an experiment UID — the standard post-injection probe
# inside a tool-pod exec. Task-5193538b: ``blade status --uid ...`` was
# recorded as a kubectl-native INJECTION because ``blade`` was in neither
# vocabulary and the fail-safe below judged it mutating.
_BLADE_READONLY_VERBS = frozenset({"status", "query", "version", "-h", "--help"})
# ``ip`` is also TWO-LEVEL: ``ip [OPTIONS] OBJECT VERB [ARGS]``. Both levels
# are prefix-matched by iproute2's ``matches()`` (lib/utils.c — true when the
# given token is a prefix of the keyword) and both take the FIRST match. The
# object table is in ip.c cmds[] ORDER, which is why ``ip s`` is the ``sr``
# object rather than ``stats`` — and why the object token may never be fed to
# the verb scan (``ip r`` would read as ``replace``).
# The verb verdict uses the same first-match rule against the OBJECT's own
# dispatch chain, and that order is exactly what makes the single letter ``s``
# mean two different things:
#   - iplink.c    matches set/change BEFORE show          → ``ip l s``  = link set
#   - ipaddress.c matches list/show BEFORE save/flush     → ``ip a s``  = addr show
#   - iproute.c   matches list/show BEFORE save           → ``ip r s``  = route show
#   - iprule.c    matches list/lst/show FIRST             → ``ip ru s`` = rule show
#   - ipneigh.c   matches show BEFORE flush               → ``ip n s``  = neigh show
# so ``s`` is allowed ONLY for the objects whose chain resolves it to a
# display; everything else falls through to the ``set`` reading (a mutation) or
# to "no such verb", and a probe must not rely on either.
_IP_OBJECTS = (
    "address",
    "addrlabel",
    "maddress",
    "route",
    "rule",
    "neighbor",
    "neighbour",
    "ntable",
    "ntbl",
    "link",
    "l2tp",
    "fou",
    "ila",
    "macsec",
    "tunnel",
    "tunl",
    "tuntap",
    "tap",
    "token",
    "tcpmetrics",
    "tcp_metrics",
    "monitor",
    "xfrm",
    "mroute",
    "mrule",
    "netns",
    "netconf",
    "vrf",
    "sr",
    "nexthop",
    "mptcp",
    "ioam",
    "help",
    "stats",
)
# Every entry is a verb that CHANGES state, sourced from the dispatch chains
# rather than from memory: add/change/chg/replace/prepend/append/test
# (iproute.c — ``test`` is iproute_modify(RTM_NEWROUTE, NLM_F_EXCL), i.e. it
# CREATES the route, and ``prepend``/``restore`` were missing entirely),
# delete, flush, set, exec (``ip netns exec``/``ip vrf exec`` run an arbitrary
# command). ``save`` is NOT here on purpose: it is the binary dump to STDOUT
# (iproute.c save_route_prep writes its magic to STDOUT_FILENO).
_IP_MUTATING_VERBS = (
    "add",
    "append",
    "change",
    "chg",
    "delete",
    "exec",
    "flush",
    "prepend",
    "replace",
    "restore",
    "set",
    "test",
)
# Objects whose chain resolves the bare ``s`` to a DISPLAY verb (evidence:
# the order list above). Anything unlisted is refused for ``s``: its chain
# either prefers ``set`` (link) or has no ``s`` verb at all, and both readings
# are cheaper to refuse than to guess.
_IP_S_DISPLAY_OBJECTS = frozenset(
    {"address", "route", "rule", "neighbor", "neighbour"}
)
# ip's global options are prefix-matched too and ONE leading dash is stripped
# (ip.c: ``if (opt[1] == '-') opt++;`` then ``matches(opt, "-family")``), so
# ``--family`` and ``-family`` are the same option. Only these consume a
# separate value; a miss here shifts the parser's idea of which token is the
# OBJECT (``ip -f inet a``).
_IP_GLOBAL_VALUE_TOKENS = frozenset(
    {"-l", "-loops", "-f", "-family", "-b", "-batch", "-rc", "-rcvbuf", "-n", "-netns"}
)
_SYSTEMCTL_READONLY_VERBS = frozenset(
    {
        "status",
        "is-active",
        "is-enabled",
        "is-failed",
        "is-system-running",
        "show",
        "cat",
        "list-units",
        "list-unit-files",
        "list-dependencies",
        "list-timers",
        "list-sockets",
        "list-jobs",
    }
)
# mount's write faces, diffed against mount(8)'s SYNOPSIS. They range over
# BOTH spellings — ``-o``/``--options`` (and the attached ``-orw``), the
# bind/move/``--make-*`` propagation forms, ``--remount`` and ``--all`` —
# one of which is ``--source``: ``mount --source=A --target=B`` mounts
# WITHOUT any positional, so the source option alone decides (``--target``
# alone is a display form and stays covered by the positional check) — and
# it is judged by getopt_long's prefix rule, because ``--sour=/dev/sda1``
# was the one form the old exact/head matches let through (R42).
_MOUNT_MUTATING_LONG_FLAGS = (
    "--options",
    "--bind",
    "--rbind",
    "--move",
    "--make",
    "--remount",
    "--source",
    "--all",
)
# ``-a``/``-B``/``-M`` also bundle (``mount -av``/``mount -aB``), so the
# cluster is scanned.
_MOUNT_MUTATING_SHORT_CHARS = frozenset("aBM")
_MOUNT_VALUELESS_SHORT = frozenset("avrwnfli")
_DMESG_MUTATING_LONG_FLAGS = ("--clear", "--read-clear")
# ``-C``/``-c`` bundle with the display flags (``dmesg -cT`` clears AND prints).
_DMESG_MUTATING_SHORT_CHARS = frozenset("Cc")
# Mechanical diff against util-linux dmesg(1)'s SYNOPSIS (R42): S (--syslog),
# W (--follow-new), D (--console-off) and E (--console-on) are no-argument
# flags too — omitting them stopped the cluster scan AT the unknown flag and
# hid a write behind it (``dmesg -Wc`` clears the ring buffer while the
# cluster was read as "W" and never saw the c).
_DMESG_VALUELESS_SHORT = frozenset("CcTxkurtHwdePSDEW")
# journalctl reads the journal; only its maintenance verbs write to it.
# The table holds FULL names judged by getopt_long's prefix rule (``--rot``
# IS ``--rotate``) — an exact-match table read ``--rot`` as a harmless
# unknown and waved it through (R42).
_JOURNALCTL_MUTATING_LONG_FLAGS = (
    "--rotate",
    "--flush",
    "--sync",
    "--relinquish-var",
    "--vacuum-size",
    "--vacuum-time",
    "--vacuum-files",
)
# sysctl reads unless a write form is present: ``-w``, ``key=value``, loading
# from a file (``-p`` or its exact alias ``-f`` — "Alias of -p", sysctl(8)),
# or applying every config file (``--system``). ``-p[FILE]`` takes an OPTIONAL
# value and the manual's own EXAMPLE is the attached spelling
# (``sysctl -p/etc/sysctl.conf``), so the short form is judged through the
# cluster scan; ``-f`` was missing from this table entirely (fail-open).
_SYSCTL_MUTATING_LONG_FLAGS = ("--write", "--load", "--system")
_SYSCTL_MUTATING_SHORT_CHARS = frozenset("wpf")
# Valueless short flags, mechanically diffed against sysctl(8)'s PARAMETERS:
# every one names a read form or does nothing (-o/-x "exists for BSD
# compatibility", -A/-X alias -a, -d alias -h). ``w``/``p``/``f`` are absent on
# purpose — they are the mutations (and must end the cluster scan, since a
# value may ride them); ``r`` is absent too because it takes a pattern.
_SYSCTL_VALUELESS_SHORT = frozenset("neNqaAbdXoxhV")
# date reads the clock unless it SETS it: ``-s``/``--set`` change the system
# time — which in this project is itself a fault (clock skew), never a probe.
# ``date -s`` appears verbatim in the time-drift skill, so misjudging it as
# read-only would wave an injection through the verify/intent screeners.
# The short side is date.c's optstring verbatim, "d:f:I::r:Rs:u": the valueless
# letters are exactly R/u, ``I`` takes an OPTIONAL value (that is what keeps
# ``-Iseconds`` from reading its tail as flags), and d/f/r consume a token —
# which is what makes the ATTACHED write ``-s091712342025`` findable by the
# cluster scan while the long spelling is found by the prefix match (``--se``).
_DATE_MUTATING_LONG_FLAGS = ("--set",)
_DATE_MUTATING_SHORT_CHARS = frozenset("s")
_DATE_VALUELESS_SHORT = frozenset("Ru")
# ``-d``/``-f``/``-r`` consume the next token (``date -d yesterday``): without
# consuming it, the value would be mistaken for the POSIX set operand below.
# The long spellings are matched by prefix for the same reason (``--da
# yesterday`` is the same request, and without the skip "yesterday" would be
# read as a positional — a false rejection, not a miss).
_DATE_VALUE_SHORT_FLAGS = frozenset({"-d", "-f", "-r"})
_DATE_VALUE_LONG_FLAGS = ("--date", "--file", "--reference")
# route prints/-n unless it edits the table (``add``/``del``/``delete``/
# ``flush``) — the mutating verbs are positionals, not flags.
_ROUTE_MUTATING_VERBS = frozenset({"add", "del", "delete", "flush", "change"})
# ethtool inspects (bare / ``-i``/``-S``/``-k``/``-g``/``-a``/``-c``) unless a
# CHANGE flag is present. Table mechanically diffed against ethtool(8)'s
# SYNOPSIS, which corrects two errors of the previous version: ``-P`` is
# ``--show-permaddr`` (a DISPLAY — it was refused, and the ``--set-eeprom``
# name it was refused under does not exist), while the EEPROM writer is
# ``-E``/``--change-eeprom`` (it was missing). Also missing were the
# disruptive forms a NIC drill actually reaches for: -r/--negotiate (restarts
# auto-negotiation — a link flap), -t/--test (the offline self-test takes the
# adapter down), -f/--flash + --flash-module-firmware, -W/--set-dump,
# -N/-U/--config-nfc/--config-ntuple (the SYNOPSIS carries a ``delete N``
# form), -X/--set-rxfh-indir and the --set-* hardware setters.
# R42 re-diff against the FULL SYNOPSIS added the setters that were still
# missing — --set-tunable, --set-fec, --set-module, --set-plca-cfg, --set-mm,
# --set-pse — plus -Q/--per-queue (a per-queue WRITE form the short table
# missed entirely).
# EVERY ethtool short option is valueless — the arguments are positional
# keywords, not getopt values — so a bundled token is scanned in full
# (``-seth0`` reads 's' here, and the real binary rejects that spelling
# anyway); that is why the valueless table lists every short letter.
_ETHTOOL_MUTATING_SHORT_CHARS = frozenset("ACGKNUXEfLWrtsQ")
_ETHTOOL_VALUELESS_SHORT = frozenset("hacgidekpPSnuwTxlmIACGKNUXEfLWrtsQ")
_ETHTOOL_MUTATING_LONG_FLAGS = (
    "--change",
    "--change-eeprom",
    "--pause",
    "--coalesce",
    "--features",
    "--offload",
    "--set-ring",
    "--set-channels",
    "--set-dump",
    "--config-nfc",
    "--config-ntuple",
    "--set-rxfh-indir",
    "--rxfh",
    "--negotiate",
    "--test",
    "--flash",
    "--flash-module-firmware",
    "--set-priv-flags",
    "--set-eee",
    "--set-phy-tunable",
    "--set-hwtimestamp-cfg",
    "--set-tunable",
    "--set-fec",
    "--per-queue",
    "--set-module",
    "--set-plca-cfg",
    "--set-mm",
    "--set-pse",
    "--reset",
)
# ``-d``/--register-dump and ``-w``/--get-dump READ hardware, but their
# SYNOPSIS carries an optional sink (``[file name]`` / ``[data filename]``) —
# the bare keyword plus a path is a write face, so those two flags are judged
# on the keyword (below) instead of being refused outright.
_ETHTOOL_DUMP_FLAGS = ("--register-dump", "--get-dump")
_ETHTOOL_DUMP_SHORT_FLAGS = frozenset({"-d", "-w"})
_ETHTOOL_DUMP_SINK_KEYWORDS = frozenset({"file", "data"})
# conntrack reads with ``-L``/``-S``/``-G``/``-E``/``-C``; everything that
# WRITES the table is refused. Table diffed against conntrack(8)'s option
# list, which the previous version had missed twice: ``-A``/``--add`` adds an
# entry, and ``-R``/``--load-file`` loads entries FROM a file — a read on the
# file side, but it is the connection-tracking table that changes.
_CONNTRACK_MUTATING_LONG_FLAGS = (
    "--delete",
    "--flush",
    "--update",
    "--create",
    "--add",
    "--load-file",
)
# ``z`` (--zero) atomically zeroes the table counters and is only valid
# beside ``-L`` — a write the previous table missed (R42); it rides the
# cluster scan (``conntrack -Lz``).
_CONNTRACK_MUTATING_SHORT_CHARS = frozenset("DFUIARz")
# The action/filter letters that do NOT consume the rest of a cluster: the
# actions ride along so conntrack's own documented bundle spelling (``-DF``,
# ``-DF -p tcp``) is judged, and ``n``/``g``/``j`` are its only valueless
# filters. Value-taking filters (``-p``/``-s``/``-d``/``-w``/``-m``/...) stay
# out so a cluster scan never reads their value as flags, and ``z`` stays out
# too (it is judged by the mutating scan above).
_CONNTRACK_VALUELESS_SHORT = frozenset("LGSECDFUIARngj")
# swapon ENABLES swap by default (a mutation); only ``-s``/``--show``/
# ``--summary`` are the read-only listing form. ``swapoff`` is never read-only.
# The long spellings are judged by getopt_long's prefix rule (``--sh`` IS
# ``--show``, ``--sum`` IS ``--summary`` — R42); the exact-match table refused both.
_SWAPON_READONLY_SHORT = frozenset({"-s"})
_SWAPON_READONLY_LONG = ("--show", "--summary")
# arp prints the cache unless it EDITS it. Short flags diffed mechanically
# against arp(8)'s SYNOPSIS: v/n/a/e/D are valueless, H/i/f take values, and
# the three writers are -d (delete entry), -s (add static entry) and -f
# (batch-load entries FROM a file; verified live: it OPENS the file). ``-Ds``
# (the interface-MAC form) is covered by the cluster scan because ``D`` is
# valueless and ``s`` is therefore reachable.
_ARP_MUTATING_LONG_FLAGS = ("--delete", "--set", "--file")
_ARP_MUTATING_SHORT_CHARS = frozenset("dsf")
_ARP_VALUELESS_SHORT = frozenset("vnaeD")
# hostname's valueless read flags, mechanically diffed against the net-tools
# SYNOPSIS (Ubuntu focal manpage): a/d/f/h/i/s/y are the GET group; A
# (all-fqdns) / I (all-ip-addresses) / V (version) are valueless too (R41).
# Only -F/--file takes a value. ``b`` is valueless BUT belongs to the
# SET-NAME synopsis group — its plain form writes the hostname (sethostname)
# — so it must NOT enter this table (that would wave ``-qb``/``-ab``
# through); instead the guard judges the cluster for BOTH ``b`` and ``F``:
# ``-b``'s cluster is "b" (refused), ``-bF`` truncates at b yet "b" is still
# seen (refused), and ``-qb`` never reads as a plain value (R41). ``F`` must
# stay ABSENT so a bundled ``-aF`` truncates at F and the cluster scan sees
# it (R39). ``n``/``o``/``q`` are in NO synopsis but stay fail-closed: they
# keep ``-oF``/``-qb``-grade unknown bundles inside the scan — without ``q``
# the unknown head truncates the cluster and hides ``b`` (``-qb``, R41);
# without ``a`` (dropped during the R41 edit) ``-aF/etc/hn`` read ``a`` as a
# value option and waved the attached write value through.
_HOSTNAME_VALUELESS_SHORT = frozenset("AadfhIinqosvyV")
# The two long write faces; judged by getopt_long's prefix rule (``--bo`` IS
# ``--boot``, ``--f`` IS ``--file`` — R42), unlike the previous exact match.
_HOSTNAME_MUTATING_LONG_FLAGS = ("--boot", "--file")
# --- Extended dual-use probe guards (audit follow-up) ---------------------
# ifconfig DISPLAYS by default; it mutates only when an action keyword or a
# value positional (what is being SET) is present. ``ifconfig eth0`` /
# ``ifconfig -a`` are display forms; ``ifconfig eth0 down`` /
# ``ifconfig eth0 10.0.0.1 netmask ...`` change state.
_IFCONFIG_MUTATING_KEYWORDS = frozenset(
    {
        "up",
        "down",
        "arp",
        "-arp",
        "promisc",
        "-promisc",
        "multicast",
        "mtu",
        "netmask",
        "dstaddr",
        "broadcast",
        "metric",
        "media",
    }
)
# crontab INSTALLS a crontab by default; only ``-l``/``--list`` reads.
# (``-r`` removes, ``-e`` edits, a positional file installs — all mutate.)
# The long spelling is judged by getopt_long's prefix rule (``--lis`` — R42).
_CRONTAB_READONLY_SHORT = frozenset({"-l"})
_CRONTAB_READONLY_LONG = ("--list",)
# timedatectl reads unless it SETS the clock — set-time IS the clock-drift
# fault in this project, so it must never pass as a probe.
_TIMEDATECTL_MUTATING_VERBS = frozenset(
    {
        "set-time",
        "set-timezone",
        "set-local-rtc",
        "set-ntp",
    }
)
# resolvectl / systemd-resolve read with ``status``; the set-*/revert/flush
# verbs rewrite resolver state (a network mutation).
_RESOLVECTL_MUTATING_VERBS = frozenset(
    {
        "revert",
        "set-dns",
        "set-domain",
        "set-llmnr",
        "set-mdns",
        "set-dns-over-tls",
        "set-dnssec",
        "flush-caches",
        "reset-statistics",
        "reset-server-features",
    }
)
# fdisk / parted list partitions only with ``-l``/``--list``; a bare device
# argument opens the interactive (mutating) partition editor. The long
# spelling is judged by getopt_long's prefix rule (``--lis`` — R42).
_DISK_READONLY_SHORT = frozenset({"-l"})
_DISK_READONLY_LONG = ("--list",)
# openssl is a crypto toolkit — only the ``version`` subcommand is a probe;
# every other subcommand computes / writes / connects. java RUNS bytecode by
# default; only its version banner is a safe probe (note the single-dash
# ``-version``, not covered by the ``--version`` metadata rule).
_JAVA_READONLY_PROBES = frozenset(
    {
        "-version",
        "--version",
        "-showversion",
        "-fullversion",
    }
)
# Package managers: query forms read, everything else installs / removes.
# dpkg's long query forms are judged by getopt_long's prefix rule (``--listf``
# IS ``--listfiles``, ``--stat`` IS ``--status`` — R42); dpkg's own option
# table is CASE-SENSITIVE (-l vs -L), which the literal sets preserve.
_DPKG_READONLY_SHORT = frozenset({"-l", "-s", "-S", "-L", "-W", "-p"})
_DPKG_READONLY_LONG = (
    "--list",
    "--status",
    "--search",
    "--listfiles",
    "--show",
    "--print-avail",
)
# Three name-only allowlist binaries keep one argument-level WRITE face each,
# judged by their own guards below: ``dmidecode --dump-bin FILE`` dumps the DMI
# table to a binary file (man: "dump the DMI data to a file in binary form";
# ``--from-dump`` reads one back and ``--dump``/``-u`` print hex to STDOUT —
# that one is excluded in the judge), ``file -C/--compile`` writes the compiled
# magic database (.mgc), ``blkid -g/--garbage-collect`` rewrites the blkid
# cache (man: "perform a garbage collection pass on the blkid cache").
_DMIDECODE_WRITE_LONG_FLAGS = ("--dump-bin",)
_FILE_COMPILE_LONG_FLAGS = ("--compile",)
_BLKID_WRITE_LONG_FLAGS = ("--garbage-collect",)
_APK_READONLY_VERBS = frozenset(
    {
        "info",
        "search",
        "list",
        "policy",
        "version",
        "audit",
        "manifest",
    }
)
# find — read-only only WITHOUT its action primitives. ``-exec``/``-ok`` run an
# arbitrary command per match (the ``+`` terminator needs no shell metachar,
# so the string-level screens cannot see it), ``-delete`` removes whole trees,
# ``-fprint*``/``-fls`` write result files.
_FIND_MUTATING_FLAGS = frozenset(
    {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fls",
        "-fprint",
        "-fprint0",
        "-fprintf",
    }
)
# awk is a programming language, not a text filter: ``system(...)`` runs an
# arbitrary command, and ``-f``/``-i``/``@load`` execute program FILES.
# gawk's ``-E``/``--exec`` executes a program FILE exactly like ``-f`` (the
# CGI-safe spelling); omitting it leaves the same arbitrary-program channel
# one flag over.
_AWK_MUTATING_FLAGS = frozenset(
    {
        "-f",
        "--file",
        "-i",
        "--include",
        "-E",
        "--exec",
        # gawk profiling writes a file; the bare words catch ``-W exec=``/
        # ``-W source=``/``-W profile=``/``-W dump-variables=``/
        # ``-W pretty-print=`` values (the ``-E``/``--exec`` spelling was
        # legislated, its ``-W`` spelling was not; the two value-less ``-W``
        # forms also WRITE their default files — R42).
        "-p",
        "--profile",
        "exec",
        "profile",
        "source",
        "dump-variables",
        "pretty-print",
    }
)
# Short forms take an ATTACHED value too (``-f/tmp/prog.awk``), which an
# ``-l``/``--load`` loads a gawk extension .so — ``dl_load()`` runs ARBITRARY
# native code, the same class as find's ``-exec`` (R41). ``-d``/
# ``--dump-variables`` and ``-o``/``--pretty-print`` WRITE files (awkvars.out /
# awkprof.out; R41). ``-p`` is refused outright: gawk's profile file IS its
# only -p meaning (BWK/mawk have no read-only -p). ``-l``/``-d``/``-o`` are
# refused likewise — POSIX/BWK/mawk have no read-only meaning for them, so a
# refusal can only hit gawk's write faces. ``-g``/``--gen-pot`` stays ALLOWED:
# it writes the .pot template to STDOUT (R41 reversal — R39 had it as a
# writer). ``-W`` is NOT in this tuple: gawk's -W feature set hides write
# faces among read-only switches, so it is judged per FEATURE instead (below)
# — refusing the whole ``-W`` channel also refused ``-W version``/``-W lint``,
# which are legitimate read-only probes (R40).
_AWK_MUTATING_SHORT_PREFIXES = ("-f", "-i", "-E", "-p", "-l", "-d", "-o")
# gawk long options may be abbreviated to any unique prefix ("--lo" IS
# --load), so the table stores the FULL names and the judge applies
# getopt_long's prefix rule (``_long_flag_hit``). R41 listed only the
# hand-picked heads --lo/--dump/--pretty, which missed every other unique
# abbreviation (R42). Every entry is a write face from gawk's manual:
# --load dl_load()s native code, --file/--include/--exec load program
# files, --profile/--dump-variables/--pretty-print write awkprof.out /
# awkvars.out.
_AWK_MUTATING_LONG_FLAGS = (
    "--load",
    "--file",
    "--include",
    "--exec",
    "--profile",
    "--dump-variables",
    "--pretty-print",
)
# gawk's -W write faces are a CLOSED set (its manual): exec= and source=
# execute program files, profile[=file] writes awkprof.out, and
# dump-variables[=file] / pretty-print[=file] write awkvars.out /
# awkprof.out — with or WITHOUT the ``=file`` (the bare forms use the default
# names), which is why the regex matches those two by head. Everything else
# (-W version/lint/posix/...) is read-only and stays allowed; there is NO
# ``-W gen-po`` feature and ``--gen-pot`` writes to STDOUT, so neither is a
# judge here (R41 correction). Feature lists are comma-separated
# (``-Wlint,exec=x``), so the comma prefix keeps ``lint`` from hiding the
# judge. The bare ``exec``/``source``/``profile``/``dump-variables``/
# ``pretty-print`` words in _AWK_MUTATING_FLAGS catch the standalone spelling
# (``-W exec=x``: the value token hits via the split-head match).
_AWK_W_MUTATING_FEATURE = re.compile(
    r"(?:^|,)(?:exec=|source=|profile|dump-variables|pretty-print)"
)
_AWK_MUTATING_RE = re.compile(r"system\s*\(|@load|@include")
# In-program constructs are judged on MERIT, not on characters. Only two
# constructs inside an awk program make it non-read-only:
#   print/printf ... > / >> target   writes or appends a file
#   ... | command / |& coproc         executes a command (incl. cmd | getline)
# Everything else the legacy raw-string screens used to refuse is read-only
# and stays allowed: comparisons (``NR>1``, ``print (a>b)``), string/regex
# CONTENT (``"a>b"``, ``/a|b/``), comments, logical ``||``, ``-F'|'`` field
# separators, and input redirection — ``getline < file`` only READS, the same
# capability ``cat`` already has on this allowlist. awk's own grammar makes
# the precise call possible: an unparenthesized ``>`` in a print statement IS
# the redirect operator (a comparison must be parenthesized to print), and a
# bare ``|`` at statement level has no meaning other than a command pipe.
# See _awk_program_mutation / _awk_program_arg_mutation below.
# Statement keywords: a ``/`` right after one opens a regex constant (no left
# operand is possible); after an identifier it is division.
_AWK_STATEMENT_KEYWORDS = frozenset(
    {
        "print",
        "printf",
        "getline",
        "if",
        "else",
        "while",
        "for",
        "do",
        "switch",
        "case",
        "default",
        "break",
        "continue",
        "next",
        "nextfile",
        "exit",
        "return",
        "delete",
        "function",
        "func",
        "BEGIN",
        "END",
        "in",
    }
)
# awk option rules for locating the program WORD: ``-F``/``-v`` carry inert
# string values (skipped), ``-e``/``--source`` carry program text. The long
# spellings are judged by getopt_long's prefix rule (``--sour`` IS
# ``--source`` — R42: the exact-match form let a program riding ``--sour=...``
# skip the construct scan entirely, a fail-open), and an attached value
# (``--assign=x``) must not swallow the token that follows it.
_AWK_VALUE_SHORT = frozenset({"-F", "-v"})
_AWK_VALUE_LONG_FLAGS = ("--field-separator", "--assign")
_AWK_PROGRAM_SHORT = frozenset({"-e"})
_AWK_PROGRAM_LONG_FLAGS = ("--source",)
# Short options that take NO value, per binary. Needed to read a bundled
# cluster correctly: an option that TAKES a value swallows the rest of the
# token as that value, so ``-XGET`` is "method GET", not "flags X/G/E/T".
# Scanning a cluster past such an option produces false positives (``-XGET``
# contains 'T'), so ``_reachable_cluster`` stops there — see that helper.
# Under-listing is NOT harmless either: the scan stops AT the unlisted flag
# and never sees the write flags bundled behind it (``curl -lo out`` read as
# "l" until ``l`` was listed — R42; the same shape hid ``dmesg -Wc``). Every
# valueless flag is therefore listed, mechanically diffed against each
# tool's SYNOPSIS; only VALUE-taking flags stay out.
_CURL_VALUELESS_SHORT = frozenset("sSILkvfigGNjpZqnBRJM46hV0123#al")
_WGET_VALUELESS_SHORT = frozenset("qvdbcNSkKmrpxEHn46hVF")
# curl — read-only only when the response stays on STDOUT (its default). The
# long options below write local files (``--output*``/``--cookie-jar``/
# ``--dump-header``/``--trace*``/``--remote-name``/``--stderr``/``--libcurl``/
# ``--hsts``/``--alt-svc``/``--etag-save`` — the last five were found by
# reading every file-writing option out of curl(1), R42) or move data off-box
# (``--data*``/``--json``/``--form*``/``--upload*``/``--config``).
#
# The table holds FULL names and the judge matches EXACTLY: curl's parser is
# its own and does NOT abbreviate (verified live: ``curl --out`` fails with
# "option --out: is unknown"), so a prefix rule here would refuse the
# read-only ``--cookie`` on the ``--cookie-jar`` head. Every write face of
# curl 8.x is listed individually — the old head-prefix table relied on
# family heads (``--data`` covering ``--data-binary``), which exact matching
# cannot do.
_CURL_MUTATING_LONG_FLAGS = (
    "--output",
    "--output-dir",
    "--remote-name",
    "--remote-name-all",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-ascii",
    "--data-urlencode",
    "--json",
    "--form",
    "--form-string",
    "--form-escape",
    "--upload-file",
    "--config",
    "--cookie-jar",
    "--dump-header",
    "--trace",
    "--trace-ascii",
    "--stderr",
    "--libcurl",
    "--hsts",
    "--alt-svc",
    "--etag-save",
)
_CURL_MUTATING_SHORT_CHARS = frozenset("oOdFTKcD")
# Verb discipline: ``-X``/``--request`` names the HTTP METHOD, and a drill
# target is often a REST endpoint (the apiserver itself). DELETE/POST/PUT/
# PATCH mutate the REMOTE side with zero local footprint, which the write/
# upload scan above cannot see. Only the idempotent read verbs stay
# probe-grade; a missing verb (``-X`` at argv end) fails closed with them.
_CURL_READONLY_VERBS = frozenset({"GET", "HEAD", "OPTIONS"})
_CURL_VERB_CLUSTER = re.compile(
    r"^-[" + re.escape("".join(_CURL_VALUELESS_SHORT)) + r"]*X(.*)$"
)
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
# ``--body-*``/``--upload-file`` transmit data off-box; ``--save-cookies``/
# ``--warc-file``/``--hsts-file`` write bookkeeping files (R42). All of them
# matter even with ``--spider``, which is why they are checked before it. The
# table holds FULL names judged by getopt_long's prefix rule, and
# ``--output-document`` is deliberately ABSENT: it has the stdout form
# (``-O -``) and is judged by the dedicated document scan below instead.
# ``--method`` is its own judge (a non-read verb mutates the remote side).
_WGET_MUTATING_LONG_FLAGS = (
    "--output-file",
    "--append-output",
    "--post-data",
    "--post-file",
    "--body-data",
    "--body-file",
    "--upload-file",
    "--save-cookies",
    "--warc-file",
    "--hsts-file",
)
_WGET_METHOD_LONG_FLAGS = ("--method",)
# ``-o``/``-a`` are the log sinks. The cluster scan (not an exact token match)
# is what catches the attached spellings ``-olog``/``-aolog`` (R42).
_WGET_MUTATING_SHORT_CHARS = frozenset("oa")
# wget metadata-only flags: they print and exit BEFORE any URL parsing, so no
# network access and no file write can happen (same exemption shape as
# iptables/nft/blade above). Checked only AFTER the mutating-flag scan, so a
# write/upload form stays refused no matter what rides alongside it. The long
# spellings are judged by getopt_long's prefix rule (``--vers``/``--hel`` — R42).
_WGET_METADATA_SHORT = frozenset({"-V", "-h"})
_WGET_METADATA_LONG = ("--version", "--help")
# ``--spider`` is the read-only fetch form and takes the prefix rule too
# (``--spid`` — R42; wget's own ambiguity handling rejects the shared heads).
_WGET_SPIDER_LONG_FLAGS = ("--spider",)
# ``--output-document`` is judged by the dedicated stdout scan below (see it)
# and NOT by the mutating table: ``-O -`` / ``-O /dev/null`` make it a
# stdout/discard sink. Prefix rule applies (``--output-do`` — R42).
_WGET_DOCUMENT_LONG_FLAGS = ("--output-document",)
# Universal metadata probes: GNU-style tools print and exit BEFORE any action,
# so an argv made ONLY of these flags touches neither disk, network, nor
# process state — for ANY binary. The "every token is a metadata flag" shape is
# what keeps this bypass-proof: ``docker --version run alpine`` or
# ``blade --version create cpu`` carry a real token and fall through to the
# per-binary judges. Applied below the escape-primitive check, so
# nsenter/chroot/unshare stay refused even as bare probes.
_METADATA_FLAGS = frozenset({"--version", "-V", "--help", "-h"})
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
# The write flag is read through ``_reachable_cluster`` (R39): a head-prefix
# or exact-token check misses a BUNDLED ``-mo``/``-Ao`` — getopt applies the
# tail flag just the same. ``--compress-program`` RUNS an arbitrary
# compressor (find -exec grade) and is refused on split-head match, so the
# ``=`` spelling cannot hide it.
_SORT_VALUELESS_SHORT = frozenset("bCcdfghimMnrsRsuVz")
_SORT_MUTATING_LONG_FLAGS = frozenset({"--output", "--compress-program"})
# sysstat sar, mechanically diffed against the SYNOPSIS (R40): A/B/b/C/D/d/
# F/H/h/p/q/R/r/t/U/u/v/V/x are valueless; e/f/i/n/o/s/w take values. (The
# earlier table missed h/p and mislabelled W as value-taking — ``[-W]`` is a
# standalone bracket in the synopsis, same as ``[-h]``.) ``A`` MUST be
# present — without it ``-Ao`` truncates at A and the bundled write flag
# escapes the scan; h/p for the same reason one letter over (``sar -ho f``).
_SAR_VALUELESS_SHORT = frozenset("ABbCDdFHhpqRrtUuvVx")
# ``--kill`` is matched by getopt_long's prefix rule (``--kil`` IS --kill);
# the ``-K`` short rides the cluster scan (``K`` is not in the valueless
# table, so the scan stops ON it — R42).
_SS_MUTATING_LONG_FLAGS = ("--kill",)
# ``-D``/``--diag`` dumps the raw TCP socket table into the NAMED file
# (ss(8): "Do not display anything, just dump raw information about TCP
# sockets to FILE"). The write face is the VALUE, not the flag, so the guard
# below judges the value and exempts the discard targets (R43).
_SS_WRITE_LONG_FLAGS = ("--diag",)
# ``-f``/``-N`` are omitted deliberately: they TAKE a value (family / netns), so
# listing them would let the cluster scan run into that value and reject a
# namespace named e.g. "K8s".
_SS_VALUELESS_SHORT = frozenset("tuwxnlapemios46rZzdgHSbEM")
# uniq's value flags: the short forms take a separate value (``-f 2``); the
# long ones are judged by getopt_long's prefix rule (``--skip-fiel`` IS
# ``--skip-fields`` — R42) and may carry the value attached (``--skip-fields=2``),
# which consumes nothing beyond the token itself.
_UNIQ_VALUE_SHORT = frozenset({"-f", "-s", "-w"})
_UNIQ_VALUE_LONG = ("--skip-fields", "--skip-chars", "--check-chars")
# xxd's value-taking short options (``-c 16`` / ``-s 32`` / ``-l 64`` /
# ``-n name`` / ``-o off`` / ``-g bytes`` / ``-R when``): a separate value
# token must NOT be counted as a positional (``xxd -l 64 in out`` has exactly
# two operands). Only confirmed value-takers are listed — listing a flag that
# does not take a value would swallow a real positional (R43).
_XXD_VALUE_SHORT = frozenset({"-c", "-g", "-l", "-n", "-o", "-s", "-R"})
# ping/ping6 — the traffic-amplification primitives of a binary that is
# otherwise a pure connectivity probe: ``-f`` floods (thousands of pps),
# ``-l`` preloads packets without waiting for replies, ``-i <0.1`` is a
# zero/near-zero interval (== flood), ``-p`` crafts arbitrary payload bytes
# and ``-s >1500`` sends jumbo packets beyond the standard MTU. Same category
# as ``ss -K``: a fault injection, not an observation (R37: the only miss
# family in a 30-form adversarial matrix — every other dual-use binary
# already had a guard).
_PING_FLOOD_LONG_FLAGS = frozenset({"--flood", "--preload", "--pattern"})
_PING_INTERVAL_LONG_FLAGS = frozenset({"--interval"})
_PING_SIZE_LONG_FLAGS = frozenset({"--packetsize"})
# Feeds ``_reachable_cluster`` so a bundled flood flag (``-fq``) is judged.
# Only high-confidence valueless flags are listed: a WRONG entry here makes
# the cluster scan read a value's characters as flags (fail-closed false
# rejection), a MISSING one can hide a flood flag behind it (fail-open).
# Every value-taking flag is therefore absent: c i I l m M N p Q s S t T w W.
_PING_VALUELESS_SHORT = frozenset("aAbBdDfhLnOqrRvVU46")
# arping — spoof/announce primitives of a binary whose plain form (MAC
# resolution) is a pure diagnostic: ``-U``/``-A`` ANNOUNCE the TARGET ip as
# this host's MAC (unsolicited / REPLY modes), so ``-U -S <victim> <gw>``
# poisons the gateway's ARP cache (a man-in-the-middle premise); ``-S``/
# ``-s`` forge the sender ip/MAC of any request. ``arp`` itself is EXCLUDED
# from the word-list for exactly ``-d``/``-s`` (see the table note above)
# while ``arping`` was admitted whole — those flags were simply missed
# (R38: the only miss family in its slice). ``-D`` (DAD probe, sender ip
# 0.0.0.0) and the plain ``-c``/``-f``/``-I`` probe stay read-only.
_ARPING_ANNOUNCE_SHORT_CHARS = frozenset("UA")
_ARPING_FORGE_SHORT_CHARS = frozenset("Ss")
# High-confidence valueless shorts feeding ``_reachable_cluster`` so a
# bundled ``-qU`` is judged. Wrong entries fail CLOSED (a value's chars
# read as flags), missing ones can hide a bundled U/A (fail-open) — AND,
# because the walk treats an unknown tail as a value option, a missing
# member lets the NEXT token be consumed as that value (``arping -a -U gw``
# ate ``-U`` as ``-a``'s value; R40). Mechanically diffed against the
# iputils synopsis ([-AbDfhqUV]): f/q/D/b/h + a (audible) + V (version) are
# valueless; c/w/I take values (+ U A S s, judged by the scans above). ``V``
# was added in R41 — without it the walk read ``arping -V -U gw`` as ``-V``
# taking ``-U`` as its value and waved the announce flag through.
_ARPING_VALUELESS_SHORT = frozenset("afqDbhV")
# dig — ``-f <file>`` (batch mode) encodes every line of a local file into
# DNS queries sent to a chosen server: the same "moves host data off-box"
# class as ``curl -d @/etc/shadow``, which is refused. Bulk forms are
# refused whatever the file; plain queries stay read-only (``+`` options
# and ``@servers`` are not shell options and never reach this walk).
_DIG_BULK_SHORT_CHARS = frozenset("f")
# Valueless shorts mechanically diffed against the ISC BIND synopsis
# (``dig -h`` usage itself): 4/6/i/m/u plus h (help) and v (version) — the
# earlier list was built from ``dig -h`` output yet omitted h itself, and a
# missing member lets ``dig -h -f file`` consume ``-f`` as ``-h``'s value
# (R40). Wrong entries fail closed; everything else is treated as
# value-taking: b c k p q t x y.
_DIG_VALUELESS_SHORT = frozenset("46himuv")
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
_RUNTIME_READONLY_VERBS = frozenset(
    {
        "ps",
        "images",
        "inspect",
        "inspecti",
        "inspectp",
        "logs",
        "stats",
        "statsp",
        "version",
        "info",
        "pods",
        "top",
        "imagefsinfo",
        "port",
        "events",
    }
)
# Runtime-CLI global flags that consume a separate value; their value must not
# be mistaken for the verb (e.g. ``crictl --runtime-endpoint unix://... ps``).
_RUNTIME_VALUE_FLAGS = frozenset(
    {
        "-r",
        "--runtime-endpoint",
        "-i",
        "--image-endpoint",
        "-t",
        "--timeout",
        "-c",
        "--config",
        "-H",
        "--host",
        "--context",
        "--log-level",
        "-n",
        "--namespace",
        "--address",
        "--tlscacert",
        "--tlscert",
        "--tlskey",
        "-D",
        "--debug-dir",
    }
)
# Wrappers that prefix a real command; the wrapped command decides the verdict.
_COMMAND_WRAPPERS = ("timeout", "stdbuf", "nice", "ionice", "env", "watch")
# Wrapper flags consuming a separate value — skipping only the flag would leave
# its value to be mistaken for the wrapped command.
_WRAPPER_VALUE_FLAGS = frozenset(
    {
        "-n",
        "-c",
        "-p",
        "-o",
        "-i",
        "-e",
        "-k",
        "-s",
        "-u",
        "--kill-after",
        "--signal",
        "--unset",
        "--chdir",
        "--class",
        "--classdata",
        "--pid",
        "--output",
        "--input",
        "--error",
        "--interval",
    }
)
# ``timeout``'s DURATION positional: a number with an optional unit suffix.
_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")

# Shell control operators that make an inner command a compound script rather
# than a single probe — fail closed to non-read-only. ``|`` is handled
# separately (pipeline of read-only stages is allowed for exec probes).
# B46: ``;`` / ``&&`` / ``||`` at token boundaries are additionally split as
# chain separators (``_split_chain_segments`` below) — every segment judged
# independently, same dialect as the facts engine's ``allow_chains`` and
# target_guard's ``_PROBE_SEPARATOR_OPS``. Separators embedded mid-token
# (indistinguishable from quoted literals at this layer) still hit this
# fail-closed list.
_SHELL_CONTROL_OPS = (">", "<", "`", "$(", "&&", "||", ";", "&", "\n")

# Chain separators recognised at token boundaries, longest first (so ``&&``
# is not mis-read as a background ``&``).
_CHAIN_SEPARATORS = ("&&", "||", ";")


def _split_chain_segments(tokens: list[str]) -> list[list[str]] | None:
    """Split *tokens* into ``;``/``&&``/``||``-separated segments.

    Token-layer approximation of the facts engine's chain policy: shlex
    glues a separator to the PRECEDING token's tail (``sh -c 'a; b'`` →
    ``['a;', 'b']``), so separators are recognised at token tails and as
    standalone tokens. A separator embedded MID-token cannot be told apart
    from a quoted literal at this layer (quote information is gone) — return
    ``None`` so the caller keeps the fail-closed control-operator refusal.
    ``None`` is also returned when no separator is present at all (the
    caller's unchanged single-probe path).
    """
    if not any(sep in tok for tok in tokens for sep in _CHAIN_SEPARATORS):
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _CHAIN_SEPARATORS:
            segments.append(current)
            current = []
            continue
        matched = next(
            (sep for sep in _CHAIN_SEPARATORS
             if tok.endswith(sep) and len(tok) > len(sep)),
            None,
        )
        if matched is not None:
            head = tok[: -len(matched)]
            if any(sep in head for sep in _CHAIN_SEPARATORS):
                return None  # another separator embedded mid-token
            current.append(head)
            segments.append(current)
            current = []
            continue
        if any(sep in tok for sep in _CHAIN_SEPARATORS):
            return None  # embedded mid-token — unprovable at this layer
        current.append(tok)
    segments.append(current)
    non_empty = [seg for seg in segments if seg]
    return non_empty if non_empty else None


def _split_glued_pipes(tokens: list[str]) -> list[str]:
    """Expand a ``|`` GLUED inside a token into a standalone separator token.

    shlex glues a pipe to the ADJACENT word (``ps aux|rm`` → ``['ps',
    'aux|rm']``), and at the token layer the quote information that told a
    quoted literal ``'a|b'`` apart from the real operator is gone. Without
    this expansion the glued form hid its tail stage completely: the token
    landed in ARGUMENT position (``aux|rm`` is an argument of ``ps``), the
    stage splitter only saw standalone ``|`` tokens, and the per-binary
    argument guards never look at a pipe — while the facts engine refused
    the very same payload (R44). Splitting can only ADD structure (every
    extra stage must itself pass the read-only judge), so a quoted literal
    over-denies — fail-closed, and the facts engine (which still sees the
    quotes) is the exact judge whenever raw text exists.
    """
    if not any("|" in tok and tok != "|" for tok in tokens):
        return tokens
    expanded: list[str] = []
    for tok in tokens:
        if "|" not in tok or tok == "|":
            expanded.append(tok)
            continue
        for pos, piece in enumerate(tok.split("|")):
            if pos:
                expanded.append("|")
            if piece:
                expanded.append(piece)
    return expanded


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
                i += 2  # flag consuming a separate value (``nice -n 5``)
            elif tok.startswith("-") or "=" in tok:
                i += 1  # valueless flag, or an ``env VAR=VAL`` assignment
            elif binary == "timeout" and _DURATION_RE.match(tok):
                i += 1  # timeout's DURATION positional
            else:
                break  # first real token of the wrapped command
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


def _long_flag_hit(
    token: str, flags: tuple[str, ...] | frozenset[str], *, abbreviate: bool = True
) -> str | None:
    """Return the *flags* entry *token* names, or ``None``.

    ``--flag=value`` is judged by its head, so an attached value can never
    hide the flag. With *abbreviate* (the default — every getopt_long-based
    tool) BOTH directions count, because getopt_long accepts any UNIQUE
    abbreviation of a long option (``--se`` IS ``--set``, ``--output-fi`` IS
    ``--output-file``): ``flag.startswith(name)`` covers the abbreviation,
    and ``name.startswith(flag)`` lets one family head cover its spelled-out
    siblings (``--data`` reaches ``--data-binary``). Both directions were
    checked against every table so no read-only option is caught.

    ``abbreviate=False`` is for parsers that do NOT abbreviate (curl): there
    ``--out`` is simply an unknown option, and a prefix rule would refuse the
    read-only ``--cookie`` under the ``--cookie-jar`` head — only exact
    matches count.
    """
    if not token.startswith("--") or token == "--":
        return None
    name = token.split("=", 1)[0]
    for flag in flags:
        if name == flag:
            return flag
        if abbreviate and (flag.startswith(name) or name.startswith(flag)):
            return flag
    return None


def _keyword_prefix_hit(token: str, keywords: tuple[str, ...]) -> str | None:
    """Return the *keywords* entry *token* is a PREFIX of, or ``None``.

    iproute2 decides by ``matches(cmd, pattern)`` — true when *cmd* is a
    prefix of the keyword — and every dispatch chain takes the FIRST hit.
    That order is the whole semantics of the single letter ``s``: iplink.c
    matches ``set`` before ``show`` (``ip l s`` IS a link-set), while
    ipaddress.c / iproute.c / iprule.c / ipneigh.c match their display verb
    first. Option tokens never prefix-match a keyword.
    """
    if not token or token.startswith("-"):
        return None
    for keyword in keywords:
        if keyword.startswith(token):
            return keyword
    return None


def _iproute2_positionals(args: list[str], value_tokens: frozenset[str]) -> list[str]:
    """Return *args* after the leading option run of an iproute2 tool.

    ``ip``/``tc`` consume their own global options up to the first
    non-option token, and a value option's NEXT token belongs to it — a
    naive "first non-option" reading would take ``-f inet``'s value for the
    OBJECT (``ip -f inet a``). ``ip.c`` strips ONE leading dash before
    matching (``if (opt[1] == '-') opt++;``), so ``-family`` and ``--family``
    are the same option and one table serves both spellings.
    """
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            i += 1
            break
        if not token.startswith("-") or token == "-":
            break
        i += 2 if ("-" + token.lstrip("-")) in value_tokens else 1
    return args[i:]


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


def _awk_program_mutation(program: str) -> str | None:
    """First write/execute construct in an awk program, or None.

    Quote/regex/comment/paren-aware scan that judges by CONSTRUCT, not by
    character. Only two in-program constructs make awk non-read-only:

      - an output redirect: ``>``/``>>`` at paren-depth 0 inside a
        print/printf statement (awk's own grammar makes this exact — an
        unparenthesized ``>`` there IS the redirect operator; printing a
        comparison requires parentheses, so ``print (a>b)`` stays allowed);
      - a command pipe: a bare ``|`` or ``|&`` at depth 0 — awk has no other
        single-pipe operator, so this is always ``print | cmd``,
        ``cmd | getline``, or a coproc (``||`` is logical OR, skipped).

    Input redirection (``getline < file``) only READS — the same capability
    ``cat`` has on this allowlist — and stays allowed along with
    comparisons, string/regex content, and comments. Division-vs-regex uses
    the lexer rule (a ``/`` with no possible left operand opens a regex
    constant), and a backslash-newline continuation does not end a
    statement. Anything unbalanced fails CLOSED — awk would refuse the
    program anyway.
    """
    depth = 0
    i, n = 0, len(program)
    stmt_print = False  # a print/printf statement is open at depth 0
    prev_operand = False  # previous significant char could end an operand
    while i < n:
        ch = program[i]
        if ch == "\\" and i + 1 < n and program[i + 1] == "\n":
            i += 2  # line continuation, not a stmt end
            continue
        if ch == '"':
            i += 1
            while i < n:
                if program[i] == "\\":
                    i += 2
                    continue
                if program[i] == '"':
                    break
                i += 1
            if i >= n:
                return "an unbalanced string literal"
            i += 1
            prev_operand = True  # a string is an operand
            continue
        if ch == "/" and not prev_operand:
            i += 1  # regex constant — never division here
            while i < n:
                if program[i] == "\\":
                    i += 2
                    continue
                if program[i] == "/":
                    break
                i += 1
            if i >= n:
                return "an unbalanced regex"
            i += 1
            prev_operand = True
            continue
        if ch == "#":
            j = program.find("\n", i)
            i = n if j < 0 else j  # a comment runs to end of line
            continue
        if ch in "([":
            depth += 1
            prev_operand = False
        elif ch in ")]":
            depth = max(0, depth - 1)
            prev_operand = True
        elif ch == "|" and depth == 0:
            if i + 1 < n and program[i + 1] == "|":
                i += 2  # logical OR, not a pipe
                prev_operand = False
                continue
            return "an in-program command pipe (print | cmd / cmd | getline)"
        elif ch == ">" and depth == 0 and stmt_print:
            return "an in-program output redirect (print > file)"
        elif ch in ";{}\n" and depth == 0:
            stmt_print = False
            prev_operand = False
        elif ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (program[j].isalnum() or program[j] == "_"):
                j += 1
            if depth == 0:
                word = program[i:j]
                stmt_print = word in ("print", "printf")
                prev_operand = word not in _AWK_STATEMENT_KEYWORDS
            else:
                prev_operand = True
            i = j
            continue
        elif ch.isdigit():
            prev_operand = True
        elif not ch.isspace():
            prev_operand = False  # operators/delimiters open an operand
        i += 1
    return None


def _awk_program_arg_mutation(args: list[str]) -> str | None:
    """Scan the program WORDs of an awk argv for write/execute constructs.

    Program text is located with awk's own option rules: ``-F``/``-v`` values
    are inert strings (skipped — a ``-F'|'`` separator is not a pipe),
    ``-e``/``--source`` carry program text, ``--`` ends option processing,
    and every other non-flag token is treated as program text. File and
    ``var=value`` operands are scanned too — over-scanning there only fails
    closed (a filename carrying these shapes was refused before).
    """
    i, n = 0, len(args)
    options_done = False
    while i < n:
        arg = args[i]
        if not options_done and arg.startswith("-") and arg != "-":
            if arg == "--":
                options_done = True
                i += 1
                continue
            if arg in _AWK_VALUE_SHORT or (
                _long_flag_hit(arg, _AWK_VALUE_LONG_FLAGS) is not None
            ):
                # An inert string value follows (``-v x=1``) — or rides the
                # token itself (``--assign=x=1``), consuming nothing more.
                i += 2 if "=" not in arg else 1
                continue
            if arg in _AWK_PROGRAM_SHORT or (
                _long_flag_hit(arg, _AWK_PROGRAM_LONG_FLAGS) is not None
            ):
                if "=" in arg:
                    prog = arg.split("=", 1)[1]  # --sour=<program>
                    i += 1
                else:
                    prog = args[i + 1] if i + 1 < n else ""
                    i += 2
            elif arg.startswith("-e") and len(arg) > 2:
                prog = arg[2:]  # gawk attached ``-e<program>``
                i += 1
            else:
                i += 1  # other flag / attached inert value
                continue
        else:
            prog = arg
            i += 1
        mutation = _awk_program_mutation(prog)
        if mutation is not None:
            return mutation
    return None


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
        # a bare metadata probe (``timeout -V`` / ``nice --help``) prints and
        # exits; a bare wrapper otherwise does nothing observable.
        if binary in _READONLY_BINARIES or (
            args and all(a in _METADATA_FLAGS for a in args)
        ):
            return True, None
        return (
            False,
            f"'{binary}' wraps no command, so read-only status cannot be determined",
        )

    # Escape primitives reach the host. A ``/host/...`` absolute path needs NO
    # special handling here: ``binary`` above is the BASENAME, so
    # ``/host/usr/bin/cat`` classifies as ``cat`` (a legitimate debug-pod probe
    # path) while ``/host/usr/bin/iptables -A`` still lands in the iptables
    # guard below, and an unknown ``/host`` binary fails closed at the end.
    if binary in _ESCAPE_PRIMITIVES:
        return (
            False,
            f"'{binary}' reaches the host / escapes the container, not a read-only probe",
        )

    # Pure metadata probe (``--version`` / ``-h`` / ... and nothing else): every
    # CLI prints and exits before any action, whatever the binary otherwise
    # does — covers dd/timeout/nice/systemctl/docker/crictl/stress-ng/... in
    # one rule instead of per-binary exemptions.
    if args and all(a in _METADATA_FLAGS for a in args):
        return True, None

    # Netfilter tooling — read-only only in list/version forms. The command verb
    # may be preceded by global options (``iptables -t nat -L -n``), so skip
    # them before locating the verb rather than reading args[0].
    # Evidence: even ``-L`` creates /run/xtables.lock (O_CREAT) — the iptables
    # 1.8+ lock protocol taken for ANY netlink op; the ruleset itself is not
    # touched (strace shows no other write).
    if binary in ("iptables", "ip6tables"):
        i = 0
        while i < len(args):
            a = args[i]
            if a in _IPTABLES_GLOBAL_VALUE_FLAGS:
                i += 2  # ``-t <table>`` / ``-M <modprobe>`` consume a value
                continue
            if a in _IPTABLES_GLOBAL_VALUELESS:
                i += 1
                # ``-w`` may carry an optional seconds value (``-w 5 -L``)
                if a == "-w" and i < len(args) and args[i].isdigit():
                    i += 1
                continue
            break
        cmd = args[i] if i < len(args) else ""
        if not (
            cmd in _IPTABLES_READONLY_SHORT
            or _long_flag_hit(cmd, _IPTABLES_READONLY_LONG) is not None
        ):
            return False, (
                f"'{binary}' is read-only only with -L/-S/--list/--version "
                f"(got '{cmd or 'no command'}'; -A/-D/-I/-F etc. mutate)"
            )
        # The read verb alone does not clear the line: ``-L -Z`` (and the
        # -S/--list/--list-rules spellings) is a LEGAL pair that really
        # zeroes every chain's counters (R43 — see the write-verb table
        # notes), and ``-Z`` keeps its write meaning as a SEPARATE token
        # (its argument is optional). Judge every token after the verb.
        for a in args[i + 1 :]:
            if _long_flag_hit(a, _IPTABLES_WRITE_LONG_FLAGS) is not None:
                return False, (
                    f"'{binary}' {a} combines a writing command with the"
                    " read-only one (e.g. -L -Z really zeroes the counters)"
                )
            if any(
                ch in _IPTABLES_WRITE_SHORT_CHARS
                for ch in _reachable_cluster(a, _IPTABLES_VALUELESS_SHORT)
            ):
                return False, (
                    f"'{binary}' {a} combines a writing command (-A/-D/-I/-F/"
                    "-Z...) with the read-only one, which mutates"
                )
        return True, None
    if binary == "nft":
        ok = bool(args) and (
            args[0] in _NFT_READONLY_SHORT
            or _long_flag_hit(args[0], _NFT_READONLY_LONG) is not None
        )
        if ok:
            return True, None
        return (
            False,
            "'nft' is read-only only with list/--version (add/delete/flush mutate)",
        )
    # tc is a TWO-LEVEL command (``tc [OPTIONS] OBJECT VERB`` — see the table
    # notes): the object decides which verb chain applies and is itself
    # reached by prefix, so ``tc q a`` IS ``tc qdisc add``. ``exec`` is
    # refused whatever follows it — do_exec runs an arbitrary command.
    if binary == "tc":
        positionals = _iproute2_positionals(args, _TC_GLOBAL_VALUE_TOKENS)
        obj_token = positionals[0] if positionals else ""
        obj = _keyword_prefix_hit(obj_token, _TC_OBJECTS)
        if obj == "exec":
            return (
                False,
                "'tc exec' runs an arbitrary command, which is not a diagnostic",
            )
        if obj_token and not obj_token.startswith("-") and obj is None:
            return (
                False,
                f"'tc' does not know the object '{obj_token}' (a table that"
                " trails a newer iproute2 must not become the fail-open)",
            )
        verb_token = positionals[1] if len(positionals) > 1 else ""
        verb = _keyword_prefix_hit(verb_token, _TC_MUTATING_VERBS)
        if verb is not None:
            return (
                False,
                f"'tc' {obj_token} {verb} changes traffic-control state, which"
                " mutates (only show/list queries are read-only)",
            )
        return True, None

    # blade — read-only only for experiment inspection (see _BLADE_READONLY_VERBS).
    # Evidence: even these verbs open chaosblade.dat (BoltDB bookkeeping) and
    # touch its mtime, but content stays byte-identical (md5 before/after on a
    # live node) — an open-for-mapping side effect, not a mutation.
    if binary == "blade":
        if args and args[0] in _BLADE_READONLY_VERBS:
            return True, None
        # A help flag ANYWHERE short-circuits the mutation: blade is a
        # cobra-based CLI and cobra prints help and exits before the
        # subcommand's Run executes, whatever other flags are present.
        # Verified live: `blade create mem load -h`, `blade create mem load
        # --mode ram --mem-percent 80 --timeout 10 -h`, `blade create k8s
        # node-mem load --help` and `blade destroy -h` all exit 0, print
        # usage, and leave `blade status --type create` unchanged (no
        # experiment record created). This form is the flag-discovery probe
        # for injection planning, not an injection.
        if any(a in ("-h", "--help") for a in args):
            return True, None
        return False, (
            "'blade' is read-only only for status/query/version or a -h/--help probe "
            f"(got '{args[0] if args else 'no arguments'}'; create/destroy/prepare/revoke mutate)"
        )

    # ip — a TWO-LEVEL command like tc (``ip [OPTIONS] OBJECT VERB``). The
    # object token is never fed to the verb scan (``ip r`` would read as
    # ``replace``); instead the first two tokens after the object are judged,
    # because some objects are three-level (``ip xfrm state add``,
    # ``ip mptcp endpoint add``, ``ip ioam namespace add``). The single
    # letter ``s`` is resolved by the OBJECT's own chain: it IS ``set`` for
    # link, and a display for address/route/rule/neighbor (see
    # _IP_S_DISPLAY_OBJECTS for the evidence).
    if binary == "ip":
        positionals = _iproute2_positionals(args, _IP_GLOBAL_VALUE_TOKENS)
        obj_token = positionals[0] if positionals else ""
        obj = _keyword_prefix_hit(obj_token, _IP_OBJECTS)
        if obj_token and not obj_token.startswith("-") and obj is None:
            return (
                False,
                f"'ip' does not know the object '{obj_token}' (a table that"
                " trails a newer iproute2 must not become the fail-open)",
            )
        verb_token = positionals[1] if len(positionals) > 1 else ""
        if verb_token == "s":
            if obj in _IP_S_DISPLAY_OBJECTS:
                return True, None
            return (
                False,
                f"'ip {obj_token} s' resolves to a state-changing verb for"
                " this object (only address/route/rule/neighbor read as"
                " ``show``)",
            )
        for token in positionals[1:3]:
            verb = _keyword_prefix_hit(token, _IP_MUTATING_VERBS)
            if verb is not None:
                return (
                    False,
                    f"'ip' {obj_token} {verb} changes kernel network state,"
                    " which mutates (only show/list/get queries are"
                    " read-only)",
                )
        return True, None

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
        # ``-`` is an OPERAND to getopt, not an option (same rule as uniq/xxd
        # above — R43); counting it keeps the positional judgement honest.
        positionals = [a for a in args if a == "-" or not a.startswith("-")]
        mutating = bool(positionals) or any(
            _long_flag_hit(a, _MOUNT_MUTATING_LONG_FLAGS) is not None
            or a.startswith("-o")
            or any(
                ch in _MOUNT_MUTATING_SHORT_CHARS
                for ch in _reachable_cluster(a, _MOUNT_VALUELESS_SHORT)
            )
            for a in args
        )
        if not mutating:
            return True, None
        return (
            False,
            "'mount' is read-only only with no arguments or -l (mounting/-o/-a/remount mutate)",
        )

    # dmesg — read-only unless clearing the ring buffer. The long flags are
    # matched by getopt_long's prefix rule (``--cl`` IS ``--clear``) and the
    # short ones ride the cluster scan.
    if binary == "dmesg":
        if any(
            _long_flag_hit(a, _DMESG_MUTATING_LONG_FLAGS) is not None
            or any(
                ch in _DMESG_MUTATING_SHORT_CHARS
                for ch in _reachable_cluster(a, _DMESG_VALUELESS_SHORT)
            )
            for a in args
        ):
            return (
                False,
                "'dmesg' -C/-c clears the kernel ring buffer, which mutates (read-only only reads)",
            )
        return True, None

    # journalctl — reading the journal is read-only; maintenance verbs are
    # not. The long flags are matched by getopt_long's prefix rule (``--rot``
    # IS ``--rotate``).
    if binary == "journalctl":
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _JOURNALCTL_MUTATING_LONG_FLAGS) is not None
            ),
            None,
        )
        if bad:
            return False, (
                f"'journalctl' {bad} writes to / cleans up the journal store, which mutates"
                " (read-only only queries, e.g. -u/-n/--since)"
            )
        return True, None

    # sysctl — reading a key is read-only; -w / -p (and -f, its documented
    # alias) / --system / key=value write kernel params. The short flags ride
    # the cluster scan, so ``-p/etc/sysctl.conf`` and ``-qw k=v`` are judged,
    # and the long flags are matched by getopt_long's prefix rule (``--sys``
    # IS ``--system``).
    if binary == "sysctl":
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _SYSCTL_MUTATING_LONG_FLAGS) is not None
                or any(
                    ch in _SYSCTL_MUTATING_SHORT_CHARS
                    for ch in _reachable_cluster(a, _SYSCTL_VALUELESS_SHORT)
                )
                or ("=" in a and not a.startswith("-"))
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'sysctl' {bad} writes kernel parameters, which mutates"
                " (read-only only in read form, e.g. -a / -n key / key)"
            )
        return True, None

    # hostname — bare and the read flags (-f/-s/-d/...) print a fact; a
    # POSITIONAL argument (or ``-F`` in ANY spelling — attached ``-F<file>``,
    # bundled ``-aF``, long ``--file=``) SETS the host name, which on a drill
    # node breaks kubelet identity/registration (R39: the exact-token check
    # only caught a standalone ``-F``). ``-`` counts too: getopt treats it as
    # a non-option OPERAND and net-tools calls sethname on it (R43).
    if binary == "hostname":
        bad = next(
            (
                a
                for a in args
                if a == "-"
                or not a.startswith("-")
                or _long_flag_hit(a, _HOSTNAME_MUTATING_LONG_FLAGS) is not None
                or any(
                    ch in _reachable_cluster(a, _HOSTNAME_VALUELESS_SHORT)
                    for ch in "bF"
                )
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'hostname' with an argument (or {bad}) sets the host name,"
                " which mutates node identity; only the bare form and read"
                " flags are read-only"
            )
        return True, None

    # date — reading the clock is a probe; ``-s``/``--set`` IS the clock-skew
    # fault, and so is the POSIX positional form ``date MMDDhhmm[[CC]YY][.ss]``:
    # it calls clock_settime directly (verified live). ``+FORMAT`` is the
    # display operand; ``--set=...`` is caught by prefix so the value cannot
    # hide it (R39: only ``-s``/``--set`` were checked before).
    if binary == "date":
        i = 0
        while i < len(args):
            a = args[i]
            # A value option consumes its value: the separate spelling (``-d
            # yesterday``) and the abbreviated long one (``--da yesterday``)
            # skip the NEXT token, while an attached value (``--date=y``) is
            # already inside the token and skips NOTHING — skipping again
            # would swallow the POSIX clock-set operand (``date --date=y
            # 091712342025``).
            if a in _DATE_VALUE_SHORT_FLAGS or (
                _long_flag_hit(a, _DATE_VALUE_LONG_FLAGS) is not None
            ):
                i += 2 if "=" not in a else 1
                continue
            if _long_flag_hit(a, _DATE_MUTATING_LONG_FLAGS) is not None or any(
                ch in _DATE_MUTATING_SHORT_CHARS
                for ch in _reachable_cluster(a, _DATE_VALUELESS_SHORT)
            ):
                return (
                    False,
                    f"'date' {a} sets the system clock, which mutates (that IS the clock-skew fault, not a probe)",
                )
            if not a.startswith(("-", "+")):
                return (
                    False,
                    "'date' MMDDhhmm sets the system clock (that IS the clock-skew"
                    " fault, not a probe); use +FORMAT to display",
                )
            i += 1
        return True, None

    # route — printing the table is read-only; add/del/flush edit it.
    if binary == "route":
        bad = next((a for a in args if a in _ROUTE_MUTATING_VERBS), None)
        if bad is not None:
            return (
                False,
                f"'route' {bad} edits the routing table, which mutates (only no arguments or -n listing is read-only)",
            )
        return True, None

    # ethtool — inspects by default; setter flags change the NIC. Long flags
    # are matched by getopt_long's prefix rule (``--cha`` IS ``--change``);
    # the short flags ride the cluster scan. ``-d``/``-w`` READ registers
    # unless their optional sink keyword is present, which is judged on the
    # keyword (``ethtool -d eth0 file`` writes that file).
    if binary == "ethtool":
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _ETHTOOL_MUTATING_LONG_FLAGS) is not None
                or any(
                    ch in _ETHTOOL_MUTATING_SHORT_CHARS
                    for ch in _reachable_cluster(a, _ETHTOOL_VALUELESS_SHORT)
                )
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'ethtool' {bad} changes the NIC configuration, which mutates"
                " (only no arguments or -i/-S/-k/-g/-a/-c queries are read-only)"
            )
        if any(
            _long_flag_hit(a, _ETHTOOL_DUMP_FLAGS) is not None
            or a in _ETHTOOL_DUMP_SHORT_FLAGS
            for a in args
        ) and any(a in _ETHTOOL_DUMP_SINK_KEYWORDS for a in args):
            return False, (
                "'ethtool' with a dump sink (``file``/``data``) writes the"
                " register dump into that file, which is not a read-only"
                " diagnostic (drop the keyword to dump to stdout)"
            )
        return True, None

    # conntrack — -L/-S/-G/-E/-C read; -D/-F/-U/-I/-A/-R delete, flush or
    # load the table, and -z zeroes its counters. The short actions ride the
    # cluster scan (``-DF`` is conntrack's own documented bundle spelling);
    # the long ones are matched by getopt_long's prefix rule (``--fl`` IS
    # ``--flush``).
    if binary == "conntrack":
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _CONNTRACK_MUTATING_LONG_FLAGS) is not None
                or any(
                    ch in _CONNTRACK_MUTATING_SHORT_CHARS
                    for ch in _reachable_cluster(a, _CONNTRACK_VALUELESS_SHORT)
                )
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'conntrack' {bad} deletes / flushes / zeroes the connection-tracking table, which mutates (only -L/-S/-G/-E/-C are read-only)"
            )
        return True, None

    # swapon — ENABLES swap by default; only the listing flags are read-only.
    if binary == "swapon":
        if any(
            a in _SWAPON_READONLY_SHORT
            or _long_flag_hit(a, _SWAPON_READONLY_LONG) is not None
            for a in args
        ):
            return True, None
        return (
            False,
            "'swapon' enables swap by default, which mutates (only the -s/--show listing is read-only)",
        )

    # arp — prints the cache unless -d (delete) / -s (add static) / -f
    # (batch-load from a file) edit it. The short writers ride the cluster
    # scan (``-Ds``/``-nd`` bundles) and the long ones are matched by
    # getopt_long's prefix rule (``--del`` IS ``--delete``).
    if binary == "arp":
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _ARP_MUTATING_LONG_FLAGS) is not None
                or any(
                    ch in _ARP_MUTATING_SHORT_CHARS
                    for ch in _reachable_cluster(a, _ARP_VALUELESS_SHORT)
                )
            ),
            None,
        )
        if bad is not None:
            return (
                False,
                f"'arp' {bad} edits the ARP cache, which mutates (only no arguments or -a/-n queries are read-only)",
            )
        return True, None

    # numactl — ``-H``/``--hardware`` / ``-s``/``--show`` inspect; any other form
    # RUNS a wrapped command (``numactl --physcpubind=0 stress ...``), so the
    # wrapped command must decide. Delegate exactly like the env/timeout path.
    if binary == "numactl":
        _RO_SHORT = ("-H", "-s")
        _RO_LONG = ("--hardware", "--show")
        non_opt = [a for a in args if not a.startswith("-")]
        if not non_opt:
            # No wrapped command: read-only only if every flag is an inspect
            # flag (getopt_long's prefix rule applies: ``--har`` IS
            # ``--hardware`` — R42).
            if args and all(
                a in _RO_SHORT or _long_flag_hit(a, _RO_LONG) is not None
                for a in args
            ):
                return True, None
            return (
                False,
                "'numactl' is read-only only for -H/--hardware/-s/--show queries",
            )
        if _depth >= 3:
            return (
                False,
                "'numactl' nesting is too deep to determine read-only status reliably",
            )
        # First non-option token onward is the wrapped command it runs.
        return _classify_argv(args[args.index(non_opt[0]) :], _depth + 1)

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
    # arbitrary command; ``-f``/``-i``/``@load`` execute program FILES; an
    # in-program redirect (``print > file``) or command pipe (``print | cmd``,
    # ``cmd | getline``, ``|&`` coproc) writes files or runs commands from
    # inside the program string. The in-program shapes used to be caught ONLY
    # by the string-level metachar screens on the whole-command surfaces; the
    # bashfacts engine reads the quoted program string as the literal word it
    # is, so the guard now lives at argv level, unconditionally — and it
    # judges by construct, not by character (see _awk_program_mutation), so
    # the read-only forms those screens used to refuse (``NR>1`` comparisons,
    # regex alternation, ``getline < file`` reads) stay allowed.
    if binary == "awk":
        bad = next(
            (
                a
                for a in args
                if a.split("=", 1)[0] in _AWK_MUTATING_FLAGS
                or a.startswith(_AWK_MUTATING_SHORT_PREFIXES)
                or _long_flag_hit(a, _AWK_MUTATING_LONG_FLAGS) is not None
                or (
                    a.startswith("-W")
                    and _AWK_W_MUTATING_FEATURE.search(a[2:])
                )
                or _AWK_MUTATING_RE.search(a)
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'awk' is read-only only for filtering/printing; {bad} can run commands or load program files"
                " (system()/@load/-f)"
            )
        mutation = _awk_program_arg_mutation(args)
        if mutation is not None:
            return False, (
                "'awk' is read-only only for filtering/printing; the program string carries"
                f" {mutation}, which writes a file or runs a command from inside awk"
            )
        return True, None

    # curl — read-only only when the response stays on STDOUT (the default).
    # Output/upload forms write local files or move host data off-box. Short
    # forms are read through ``_reachable_cluster`` so a bundled ``-so file``
    # is caught while a value-carrying ``-XGET`` is not mistaken for flags.
    #
    # ``-o /dev/null`` (and ``--stderr /dev/null``) is the exception: it
    # discards the body instead of writing a file, which is the standard way
    # to time a request without its payload. Recognised before the mutating
    # scan, and only for those flags — an upload or a config read stays
    # refused however its own value is spelled. The long table is matched
    # EXACTLY (``abbreviate=False``): curl does not abbreviate.
    if binary == "curl":
        args = _drop_discard_output(
            args, ("-o", "--output", "--stderr"), cluster_of=_CURL_VALUELESS_SHORT
        )
        for i, a in enumerate(args):
            verb = None
            if a in ("-X", "--request"):
                verb = args[i + 1] if i + 1 < len(args) else ""
            elif a.startswith("--request="):
                verb = a.split("=", 1)[1]
            else:
                m = _CURL_VERB_CLUSTER.match(a)
                if m:
                    # Attached (``-XDELETE``) or bundled (``-sX DELETE`` — the
                    # value rides the NEXT token when the cluster tail is bare).
                    verb = m.group(1) or (args[i + 1] if i + 1 < len(args) else "")
            if verb is not None and verb.upper() not in _CURL_READONLY_VERBS:
                return False, (
                    f"'curl' is read-only only with the idempotent verbs"
                    f" GET/HEAD/OPTIONS; {a}{' ' + verb if verb else ''}"
                    " mutates the remote endpoint"
                )
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(
                    a, _CURL_MUTATING_LONG_FLAGS, abbreviate=False
                )
                is not None
                or any(
                    ch in _CURL_MUTATING_SHORT_CHARS
                    for ch in _reachable_cluster(a, _CURL_VALUELESS_SHORT)
                )
            ),
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
    # ``-o``/``-a``/``--output-file``/``--append-output`` redirect the LOG, a
    # sink separate from the document, so they are dropped here on the same
    # discard rule: ``wget -o /dev/null -qO- <url>`` keeps both sinks off
    # disk. The document check below is untouched and still decides on its own.
    if binary == "wget":
        args = _drop_discard_output(
            args,
            ("-o", "-a", "--output-file", "--append-output"),
            cluster_of=_WGET_VALUELESS_SHORT,
        )
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _WGET_MUTATING_LONG_FLAGS) is not None
                or any(
                    ch in _WGET_MUTATING_SHORT_CHARS
                    for ch in _reachable_cluster(a, _WGET_VALUELESS_SHORT)
                )
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'wget' is read-only only with --spider or output to stdout; {bad} writes local files"
                " or uploads data"
            )
        # Metadata probes exit before any download: ``wget --version`` is the
        # standard binary-presence check and touches neither disk nor network.
        if any(
            a in _WGET_METADATA_SHORT
            or _long_flag_hit(a, _WGET_METADATA_LONG) is not None
            for a in args
        ):
            return True, None
        # ``--method`` names the HTTP verb: a store/delete verb mutates the
        # REMOTE side with no local footprint, and it does so under
        # ``--spider`` too, so it is judged before the spider exemption.
        for i, a in enumerate(args):
            method = None
            if _long_flag_hit(a, _WGET_METHOD_LONG_FLAGS) is not None:
                if "=" in a:
                    method = a.split("=", 1)[1]  # --metho=POST
                else:
                    method = args[i + 1] if i + 1 < len(args) else ""
            if method is not None and method.upper() not in _CURL_READONLY_VERBS:
                return False, (
                    f"'wget' --method {method} mutates the remote endpoint;"
                    " only GET/HEAD/OPTIONS probes are read-only"
                )
        stdout_out = False
        doc_seen = False
        for i, a in enumerate(args):
            doc_hit = _long_flag_hit(a, _WGET_DOCUMENT_LONG_FLAGS)
            if doc_hit is not None and "=" in a:
                doc_seen = True
                value = a.split("=", 1)[1]
                stdout_out = value == "-" or value in _DISCARD_SINKS
            elif a == "-O" or doc_hit is not None:
                # ``-`` is stdout; ``/dev/null`` discards. Both leave nothing on
                # disk, which is what this check is actually asking about.
                doc_seen = True
                nxt = args[i + 1] if i + 1 < len(args) else None
                stdout_out = nxt == "-" or nxt in _DISCARD_SINKS
            else:
                # Bundled cluster (``-O-`` / ``-qO-``): only when ``O`` is the
                # LAST reachable option character is the tail its value. A
                # value-carrying option earlier in the token (``-UMozillaO-``)
                # makes the trailing "O-" part of that value, not an output
                # redirect — treating it as one would call a cwd write
                # read-only.
                cluster = _reachable_cluster(a, _WGET_VALUELESS_SHORT)
                if cluster.endswith("O"):
                    doc_seen = True
                    tail = a.split("O", 1)[1]
                    # ``-qO /dev/null`` puts the sink in the NEXT token.
                    if tail == "":
                        nxt = args[i + 1] if i + 1 < len(args) else None
                        stdout_out = nxt == "-" or nxt in _DISCARD_SINKS
                    else:
                        stdout_out = tail == "-" or tail in _DISCARD_SINKS
        # A non-sink -O writes (creates/truncates) that file EVEN UNDER
        # --spider: wget opens opt.output_document with fopen("wb")
        # unconditionally (main.c) and spider only suppresses the response
        # body. Judged BEFORE the spider exemption (R43).
        if doc_seen and not stdout_out:
            return False, (
                "'wget' -O/--output-document creates or truncates that file"
                " even with --spider (only -O- (stdout) or -O /dev/null is read-only)"
            )
        if any(
            _long_flag_hit(a, _WGET_SPIDER_LONG_FLAGS) is not None for a in args
        ):
            return True, None
        if stdout_out:
            return True, None
        return False, (
            "'wget' writes the response into a file in the current directory by default; only --spider,"
            " -O- (stdout) or -O /dev/null (discard) is read-only (besides --version/--help metadata probes)"
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
            return (
                False,
                "'command' nesting is too deep to determine read-only status reliably",
            )
        ok, reason = _classify_argv(wrapped, _depth + 1)
        if ok:
            return True, None
        return False, f"'command' does not execute a read-only command: {reason}"

    # sort / sar — ``-o`` writes (and truncates) an arbitrary path. The short
    # form is read through ``_reachable_cluster`` so a BUNDLED write flag
    # (``-mo``/``-Ao``) is judged — a head-prefix check misses the cluster tail
    # (R39). ``sort --compress-program`` additionally RUNS an arbitrary
    # compressor (find -exec grade); the long match is the split-head one
    # (``=`` cannot hide it) WITH getopt_long's prefix rule, so ``--comp`` /
    # ``--out`` are the same options (R42).
    if binary == "sort":
        bad = next(
            (
                a
                for a in args
                if "o" in _reachable_cluster(a, _SORT_VALUELESS_SHORT)
                or _long_flag_hit(a, _SORT_MUTATING_LONG_FLAGS) is not None
            ),
            None,
        )
        if bad is not None:
            return (
                False,
                f"'sort' {bad} writes a file or runs a compressor, not a read-only diagnostic",
            )
        return True, None
    if binary == "sar":
        bad = next(
            (a for a in args if "o" in _reachable_cluster(a, _SAR_VALUELESS_SHORT)),
            None,
        )
        if bad is not None:
            return False, f"'sar' {bad} writes a data file, not a read-only diagnostic"
        return True, None

    # ss — ``-K``/``--kill`` force-closes every matching socket. That is a
    # fault injection, not an observation. The long spelling is matched by
    # getopt_long's prefix rule (``--kil`` IS ``--kill``, R42).
    if binary == "ss":
        # ``-D FILE`` / ``--diag=FILE`` writes the raw table to FILE — the
        # VALUE decides the verdict, and the regular discard targets stay
        # allowed (``-`` names stdout in the iproute2 dump convention).
        for i, a in enumerate(args):
            value: str | None = None
            if _long_flag_hit(a, _SS_WRITE_LONG_FLAGS) is not None:
                value = (
                    a.split("=", 1)[1]
                    if "=" in a
                    else (args[i + 1] if i + 1 < len(args) else "")
                )
            elif a.startswith("-") and not a.startswith("--"):
                cluster = _reachable_cluster(a, _SS_VALUELESS_SHORT)
                if cluster.endswith("D"):
                    tail = a.split("D", 1)[1]
                    value = (
                        tail if tail else (args[i + 1] if i + 1 < len(args) else "")
                    )
            if value is not None and value != "-" and value not in _DISCARD_SINKS:
                return (
                    False,
                    f"'ss' {a} dumps the raw socket table into '{value}',"
                    " which writes a file (use -D - or -D /dev/null to discard)",
                )
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _SS_MUTATING_LONG_FLAGS) is not None
                or "K" in _reachable_cluster(a, _SS_VALUELESS_SHORT)
            ),
            None,
        )
        if bad is not None:
            return (
                False,
                f"'ss' {bad} force-closes every matching socket, which is fault injection",
            )
        return True, None

    # ping/ping6 — traffic amplification (see the _PING_* tables). The
    # valueless table drives ``_reachable_cluster`` so a bundled flood flag
    # (``-fq``) is judged; the value walk pairs standalone, attached
    # (``-i0``) and ``=``-joined (``--interval=0``) values with their flag,
    # so the plain connectivity probe (``ping -c 4 host``) is untouched.
    if binary in ("ping", "ping6"):
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                break  # everything after it is a positional (a hostname)
            if a.startswith("--"):
                _, eq, val = a.partition("=")
                # getopt_long's prefix rule applies here too: ``--flo`` IS
                # ``--flood``, ``--pre`` IS ``--preload`` (R42).
                hit = _long_flag_hit(a, _PING_FLOOD_LONG_FLAGS)
                if hit is not None:
                    return (
                        False,
                        f"'{binary}' {a} floods the target with packets, "
                        "not a read-only diagnostic",
                    )
                hit = _long_flag_hit(a, _PING_INTERVAL_LONG_FLAGS) or _long_flag_hit(
                    a, _PING_SIZE_LONG_FLAGS
                )
                if hit is not None:
                    if not eq:
                        if i + 1 >= len(args):
                            return (
                                False,
                                f"'{binary}' {a} is missing its value",
                            )
                        i += 1
                        val = args[i]
                    try:
                        num = float(val)
                    except ValueError:
                        return (False, f"'{binary}' {a} {val} is not a readable number")
                    if hit == "--interval" and num < 0.1:
                        return (
                            False,
                            f"'{binary}' {a} {val}: a zero/near-zero interval "
                            "is a flood, not a read-only diagnostic",
                        )
                    if hit == "--packetsize" and num > 1500:
                        return (
                            False,
                            f"'{binary}' {a} {val}: packets larger than the "
                            "standard MTU are not a read-only diagnostic",
                        )
                i += 1
                continue
            if not a.startswith("-"):
                i += 1
                continue
            cluster = _reachable_cluster(a, _PING_VALUELESS_SHORT)
            if "f" in cluster:
                return (
                    False,
                    f"'{binary}' {a} floods the target with packets, "
                    "not a read-only diagnostic",
                )
            tail = cluster[-1] if cluster[-1:] else ""
            if not tail or tail in _PING_VALUELESS_SHORT:
                i += 1
                continue
            # The scan stopped at a value-taking flag: locate its value.
            # ``_reachable_cluster`` returns the option run WITHOUT the
            # leading dash, so an attached value starts at 1 + len(cluster)
            # (the ss guard only does membership checks, which is why it
            # never needed the offset).
            if len(a) > len(cluster) + 1:
                val = a[len(cluster) + 1 :]
            else:
                if i + 1 >= len(args):
                    return (False, f"'{binary}' {a} is missing its value")
                i += 1
                val = args[i]
            if tail in "lp":
                kind = (
                    "preloads packets without waiting for replies"
                    if tail == "l"
                    else "crafts arbitrary packet payloads"
                )
                return (
                    False,
                    f"'{binary}' {a} {kind}, not a read-only diagnostic",
                )
            if tail in "is":
                try:
                    num = float(val)
                except ValueError:
                    return (False, f"'{binary}' {a} {val} is not a readable number")
                if tail == "i" and num < 0.1:
                    return (
                        False,
                        f"'{binary}' {a} {val}: a zero/near-zero interval "
                        "is a flood, not a read-only diagnostic",
                    )
                if tail == "s" and num > 1500:
                    return (
                        False,
                        f"'{binary}' {a} {val}: packets larger than the "
                        "standard MTU are not a read-only diagnostic",
                    )
            i += 1
        return True, None

    # arping — spoof/announce primitives (see the _ARPING_* tables). The
    # valueless table drives ``_reachable_cluster`` so a bundled ``-qU`` is
    # judged; -S/-s are refused on sight, their value never matters.
    if binary == "arping":
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                break  # everything after it is a positional (a host)
            if not a.startswith("-") or a.startswith("--"):
                i += 1
                continue  # no long options exist; a --form just errors out
            cluster = _reachable_cluster(a, _ARPING_VALUELESS_SHORT)
            judged = cluster[-1] if cluster else ""
            if any(ch in _ARPING_ANNOUNCE_SHORT_CHARS for ch in cluster):
                return (
                    False,
                    f"'arping' {a} announces this host as an address it does "
                    "not own, which is ARP spoofing, not a read-only diagnostic",
                )
            if judged in _ARPING_FORGE_SHORT_CHARS:
                return (
                    False,
                    f"'arping' {a} forges the ARP sender identity, which is "
                    "ARP spoofing, not a read-only diagnostic",
                )
            if judged and judged not in _ARPING_VALUELESS_SHORT:
                # another value option (c/w/W/I): consume its value
                if len(a) <= len(cluster) + 1:
                    if i + 1 >= len(args):
                        return (False, f"'arping' {a} is missing its value")
                    i += 1
            i += 1
        return True, None

    # dig — ``-f`` bulk-query exfil (see _DIG_BULK_SHORT_CHARS); ``+``query
    # options and ``@servers`` never reach this walk (no leading dash).
    if binary == "dig":
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                break
            if not a.startswith("-") or a.startswith("--"):
                i += 1
                continue  # only --help/--version exist; metadata is handled above
            cluster = _reachable_cluster(a, _DIG_VALUELESS_SHORT)
            judged = cluster[-1] if cluster else ""
            if any(ch in _DIG_BULK_SHORT_CHARS for ch in cluster):
                return (
                    False,
                    f"'dig' {a} sends a local file's lines as queries to a "
                    "chosen server, which moves data off-box",
                )
            if judged and judged not in _DIG_VALUELESS_SHORT:
                # other value options (b/c/k/p/q/t/x/y): consume the value
                if len(a) <= len(cluster) + 1:
                    if i + 1 >= len(args):
                        return (False, f"'dig' {a} is missing its value")
                    i += 1
            i += 1
        return True, None

    # uniq — ``uniq INPUT OUTPUT``: the second positional is an output file.
    if binary == "uniq":
        positionals: list[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                # Everything after -- is an operand: ``-`` (stdin) or a file
                # name — both count.
                positionals.extend(args[i + 1 :])
                break
            if a in _UNIQ_VALUE_SHORT or _long_flag_hit(a, _UNIQ_VALUE_LONG) is not None:
                # ``-f 2`` consumes the next token; ``--skip-fields=2`` rides
                # its value in the token itself.
                i += 2 if "=" not in a else 1
                continue
            # ``-`` is the stdin OPERAND (getopt treats it as a non-option),
            # so ``uniq - out`` writes out — counting it as an option used to
            # let the write through (R43, verified live).
            if a == "-" or not a.startswith("-"):
                positionals.append(a)
            i += 1
        if len(positionals) > 1:
            return False, (
                f"'uniq' treats the second positional argument as an output file ({positionals[1]}), which writes to a file"
            )
        return True, None

    # xxd — ``xxd INFILE OUTFILE``: the second positional is an output file
    # (created/truncated — verified live). ``-`` is the stdin OPERAND, so
    # ``xxd - out`` writes too. Long options are refused fail-closed: their
    # value-taking shapes are not enumerated, and a value token mistaken for
    # an operand would let ``--foo X`` hide the OUTFILE.
    if binary == "xxd":
        positionals = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                positionals.extend(args[i + 1 :])
                break
            if a == "-" or not a.startswith("-"):
                positionals.append(a)
                i += 1
                continue
            if a.startswith("--"):
                return False, (
                    f"'xxd' long option {a} cannot be classified as reading or"
                    " writing, so it is refused (use the short spellings)"
                )
            if len(a) == 2 and a in _XXD_VALUE_SHORT:
                i += 2  # ``-l 64`` consumes a separate value token
                continue
            i += 1
        if len(positionals) > 1:
            return False, (
                f"'xxd' treats the second positional argument as an output file ({positionals[1]}), which writes to a file"
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
        operands = {k: v for k, _, v in (a.partition("=") for a in args) if _}
        if operands.get("of") in _DISCARD_SINKS and operands.get("if"):
            bad = next((f for f in _DD_MUTATING_OPERANDS if f in operands), None)
            if bad is None:
                return True, None
            return False, (
                f"'dd' reading into a discard sink is read-only, but {bad}= changes what is written"
            )

    # --- Extended dual-use probe guards (audit follow-up) -----------------
    # ifconfig — display unless an action keyword or a value positional (the
    # thing being SET) is present. Two+ positionals means ``iface VALUE``.
    if binary == "ifconfig":
        positionals = [a for a in args if a == "-" or not a.startswith("-")]
        kw = next(
            (p for p in positionals if p.lower() in _IFCONFIG_MUTATING_KEYWORDS), None
        )
        if kw is not None or len(positionals) >= 2:
            return False, (
                f"'ifconfig' {kw or 'with a value argument'} changes interface state "
                "(only bare / -a / single-interface display is read-only)"
            )
        return True, None

    # ipvsadm — lists with -L/--list (bare lists too); every other verb
    # adds/edits/deletes a virtual service or real server.
    if binary == "ipvsadm":
        if not args or any(
            a == "-L"
            or (a.startswith("-L") and not a.startswith("--"))
            or _long_flag_hit(a, ("--list",)) is not None
            for a in args
        ):
            return True, None
        return (
            False,
            "'ipvsadm' is read-only only with -L/--list (add/edit/delete service mutate)",
        )

    # crontab — installs/edits/removes by default; only -l/--list reads.
    if binary == "crontab":
        if any(
            a in _CRONTAB_READONLY_SHORT
            or _long_flag_hit(a, _CRONTAB_READONLY_LONG) is not None
            for a in args
        ):
            return True, None
        return (
            False,
            "'crontab' installs/edits/removes a crontab by default (only -l/--list is read-only)",
        )

    # timedatectl — reads unless it SETS the clock.
    if binary == "timedatectl":
        verb = next((a for a in args if not a.startswith("-")), "")
        if verb in _TIMEDATECTL_MUTATING_VERBS:
            return (
                False,
                f"'timedatectl' {verb} changes the clock/timezone, which mutates (status/list-timezones are read-only)",
            )
        return True, None

    # resolvectl / systemd-resolve — read with status; set-*/revert/flush mutate.
    if binary in ("resolvectl", "systemd-resolve"):
        verb = next((a for a in args if not a.startswith("-")), "")
        if verb in _RESOLVECTL_MUTATING_VERBS:
            return (
                False,
                f"'{binary}' {verb} rewrites resolver state (status is read-only)",
            )
        return True, None

    # taskset / chrt — query one pid with -p; a second positional is the value
    # being SET, and without -p they RUN a command. chrt -m lists limits.
    if binary in ("taskset", "chrt"):
        if binary == "chrt" and any(
            (a.startswith("-m") and not a.startswith("--"))
            or _long_flag_hit(a, ("--max",)) is not None
            for a in args
        ):
            return True, None
        has_p = any(
            _long_flag_hit(a, ("--pid",)) is not None
            or (a.startswith("-") and not a.startswith("--") and "p" in a[1:])
            for a in args
        )
        positionals = [a for a in args if a == "-" or not a.startswith("-")]
        if has_p and len(positionals) == 1:
            return True, None
        return (
            False,
            f"'{binary}' is read-only only as a single-pid -p query (setting affinity/priority or running a command mutates)",
        )

    # fdisk lists partitions with -l/--list (verified O_RDONLY on devices via
    # strace). parted is deliberately NOT admitted even for -l: strace shows it
    # opens every block device O_RDWR in list mode, and an RW fd on a raw
    # device is a write channel — fail closed.
    if binary == "fdisk":
        if any(
            a in _DISK_READONLY_SHORT
            or _long_flag_hit(a, _DISK_READONLY_LONG) is not None
            for a in args
        ):
            return True, None
        return (
            False,
            "'fdisk' is read-only only with -l/--list (a bare device opens the mutating partition editor)",
        )
    if binary == "parted":
        return False, (
            "'parted' opens block devices O_RDWR even in list mode (verified by strace),"
            " so no form is admitted as a read-only probe — use 'fdisk -l'"
        )

    # Three allowlist binaries whose ONLY argument-level write face the
    # name-only rule could not see (see the table notes above). Their read
    # forms remain allowed: dmidecode --dump/-u and --from-dump, file
    # -m/-f/--mime-*, blkid -i/-o/-L/-U.
    if binary == "dmidecode":
        bad = next(
            (
                a
                for a in args
                if a.split("=", 1)[0] != "--dump"  # --dump prints hex to STDOUT
                and _long_flag_hit(a, _DMIDECODE_WRITE_LONG_FLAGS) is not None
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'dmidecode' {bad} writes the DMI table to a file, which is"
                " not a read-only probe (--dump prints to STDOUT)"
            )
        return True, None
    if binary == "file":
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _FILE_COMPILE_LONG_FLAGS) is not None
                # ``-C`` BUNDLES with the display flags — ``file -bC``
                # applies the C exactly as ``-C -b`` does, so the letter
                # counts at ANY position in a short token, not only the head
                # (a full valueless table would also miss whatever flag a
                # newer file adds). The only over-refusal is an attached
                # value spelling a capital C (``-mMagic``): fail-closed.
                or (a.startswith("-") and not a.startswith("--") and "C" in a[1:])
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'file' {bad} compiles the magic database into a .mgc file,"
                " which writes (only the query forms are read-only)"
            )
        return True, None
    if binary == "blkid":
        bad = next(
            (
                a
                for a in args
                if _long_flag_hit(a, _BLKID_WRITE_LONG_FLAGS) is not None
                # Same bundle rule as file's -C above: ``blkid -pg`` runs
                # -p AND -g (getopt applies every cluster letter).
                or (a.startswith("-") and not a.startswith("--") and "g" in a[1:])
            ),
            None,
        )
        if bad is not None:
            return False, (
                f"'blkid' {bad} garbage-collects (rewrites) the blkid cache,"
                " which mutates (only the query forms are read-only)"
            )
        return True, None

    # openssl — only the 'version' subcommand is a probe.
    if binary == "openssl":
        verb = next((a for a in args if not a.startswith("-")), "")
        if verb == "version":
            return True, None
        return (
            False,
            "'openssl' is read-only only for the 'version' subcommand (other subcommands compute/write/connect)",
        )

    # java — runs bytecode; only its version banner is a safe probe.
    # Evidence (Oracle JDK 17 CDS docs): the default CDS archive is
    # memory-mapped READ-ONLY at startup; archive WRITES happen only with the
    # explicit -Xshare:dump / -XX:ArchiveClassesAtExit flags, and crash logs
    # only on abnormal exit. The banner forms below never reach bytecode.
    if binary == "java":
        if args and all(a in _JAVA_READONLY_PROBES for a in args):
            return True, None
        return (
            False,
            "'java' runs bytecode (execution); only its -version banner is a read-only probe",
        )

    # Package managers — query forms read; everything else installs/removes.
    # Evidence (strace on al8 host): rpm -q* opens BDB region files
    # (/var/lib/rpm/__db.*) O_RDWR as part of BDB env recovery, but the real
    # database (Packages/...) is opened O_RDONLY only and no DB file mtime
    # changes — query stays query.
    if binary == "rpm":
        if any(
            _long_flag_hit(a, ("--query",)) is not None
            or (a.startswith("-q") and not a.startswith("--"))
            for a in args
        ):
            return True, None
        return (
            False,
            "'rpm' is read-only only in query mode (-q/-qa/-ql..., --query); install/erase/upgrade mutate",
        )
    # dpkg query forms are classified as dpkg-query actions in the Debian man
    # page (dpkg-query reads /var/lib/dpkg without the mutating lock); no
    # Debian host exists in the test cluster, so this is doc-level evidence.
    if binary == "dpkg":
        if any(
            a.split("=", 1)[0] in _DPKG_READONLY_SHORT
            or _long_flag_hit(a, _DPKG_READONLY_LONG) is not None
            for a in args
        ):
            return True, None
        return (
            False,
            "'dpkg' is read-only only for query forms (-l/-s/-S/-L/-W); install/remove/purge mutate",
        )
    if binary == "apk":
        verb = next((a for a in args if not a.startswith("-")), "")
        if verb in _APK_READONLY_VERBS:
            return True, None
        return (
            False,
            "'apk' is read-only only for info/search/list/policy/version (add/del/upgrade mutate)",
        )

    if binary in _MUTATING_BINARIES:
        return (
            False,
            f"'{binary}' is a write/load-generating command, not a read-only diagnostic",
        )
    if binary in _READONLY_BINARIES:
        return True, None
    return False, f"'{binary}' is not a known read-only diagnostic command"


def _classify_inner(inner: list[str], _depth: int = 0) -> tuple[bool, str | None]:
    """Classify a kubectl-exec inner command (after ``--``).

    Unwraps one ``sh -c`` layer, splits ``;``/``&&``/``||`` chain segments
    (B46 — every segment must independently be read-only, mirroring the
    facts engine's ``allow_chains`` policy), fails closed on every other
    shell control operator, and requires every pipeline stage to be a
    read-only probe.

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
            return (
                False,
                f"'{entry}' nesting is too deep to determine read-only status reliably",
            )
        unwrapped = _unwrap_escape(inner)
        if not unwrapped:
            return False, (
                f"'{entry}' is followed by no parseable command, so it is treated as unsafe"
                " (a read-only probe must look like: chroot /host <read-only command>)"
            )
        ok, reason = _classify_inner(unwrapped, _depth + 1)
        if ok:
            return True, None
        return (
            False,
            f"'{entry}' does not run a read-only command once on the host: {reason}",
        )

    # B46: chain segments at token boundaries — judged per segment with the
    # full depth of this classifier (escape unwrap included, so chained
    # ``nsenter`` probes work). A separator that cannot be proven to sit at
    # a token boundary falls through to the control-operator refusal below.
    segments = _split_chain_segments(inner)
    if segments is not None:
        for seg in segments:
            ok, reason = _classify_inner(seg, _depth)
            if not ok:
                return False, f"a chained segment is not read-only: {reason}"
        return True, None

    inner_str = " ".join(inner)
    for op in _SHELL_CONTROL_OPS:
        if op in inner_str:
            return False, (
                f"contains the shell control operator '{op.strip() or op!r}'"
                " (redirect/command chain/background/substitution), which a read-only probe does not allow"
            )
    if "|" in inner_str:
        # R44: the entry test reads the JOINED text, not list membership. A
        # glued pipe lives INSIDE a token (``aux|rm`` is never the token
        # ``"|"``), so the membership test let the glued form fall past this
        # branch into ``_classify_argv`` below, where the pipe rode as an
        # ARGUMENT of the head stage (``ps aux|rm`` judged as ``ps`` with an
        # odd argument) and was admitted — while the facts engine refused the
        # identical payload. The glued pipes are expanded into separator
        # tokens FIRST so the stage splitter sees them. Order matters: the
        # expansion runs AFTER the control-operator scan above (which catches
        # ``||`` as a substring — expanding first would rewrite it to
        # ``| |`` and hide it). Splitting can only ADD stages, each of which
        # must itself pass the read-only judge, so a quoted literal such as
        # ``grep -E 'a|b'`` over-denies at this token layer (fail-closed);
        # whenever raw text exists the facts engine, which still sees the
        # quotes, is the exact judge.
        inner = _split_glued_pipes(inner)
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
                # A pipe with no command on one side (``cat /f |``, ``| df``,
                # ``a | | b``) is a shell SYNTAX error — the facts engine
                # reports it as unparseable. Skipping the empty stage judged
                # only the survivors, which admitted those shapes.
                return False, (
                    "contains a bare '|' with no command on one side, which"
                    " the shell cannot parse"
                )
            ok, reason = _classify_argv(stage)
            if not ok:
                return False, f"a pipeline stage is not read-only: {reason}"
        return True, None
    return _classify_argv(inner)


# --- Public API: bool views + reason views (single source of truth) --------


def is_readonly_argv(argv: list[str]) -> bool:
    """True if a single command (one pipeline stage) is a read-only probe."""
    return (
        _facts_verdict(
            lambda: _facts_engine().argv_rejection_reason_facts(argv),
            on_error=_INTERNAL_ERROR_REASON,
        )
        is None
    )


def is_readonly_inner_tokens(inner: list[str]) -> bool:
    """True if a kubectl-exec inner command (tokens after ``--``) is read-only."""
    return _classify_inner(inner)[0]


def readonly_inner_tokens_reason(inner: list[str]) -> str | None:
    """Specific reason a kubectl-exec inner command (tokens after ``--``) is
    NOT read-only, or ``None`` when it IS.

    The reason view of :func:`is_readonly_inner_tokens`. Callers that must
    REFUSE (the classifier's exec branch, the read-only phase screeners) use
    this view so the verdict the shared judge actually reached — e.g. "contains
    the shell control operator ';'" — survives to the model instead of being
    flattened to a boolean at the API boundary and re-invented downstream.
    """
    ok, reason = _classify_inner(inner or [])
    return None if ok else reason


# The compliant-shape guidance paired with every read-only-probe refusal.
# Single source of truth: the kubectl_read tool layer and the read-only phase
# screeners render the SAME hint, so a model refused at the screener gets the
# identical fix path it would have got from the tool (and vice versa).
READONLY_PROBE_FIX_HINT = (
    "A read-only probe is one command after `--`, or a `;`/`&&`/`||`-chained "
    "list where EVERY segment is a read-only probe. Redirects, substitution, "
    "background and heredocs still fail closed. "
    "Examples: `-- which stress-ng`, `-- cat /proc/diskstats | grep vda`, "
    "`-- echo ===T===; nsenter -t 1 -m -- df -h; nsenter -t 1 -m -- "
    "iostat -xd 1 2` (chained probes: one debug pod instead of several). "
    "If the command is genuinely a fault INJECTION, it belongs to Phase 2 "
    "(execution), not to a read-only phase."
)


def is_readonly_kubectl_exec(v_args: str) -> bool:
    """True if a ``kubectl exec``/``debug`` inner command is a read-only probe.

    Parses ``POD [-n NS] [-c C] -- INNER``, unwraps one ``sh -c`` layer, and
    treats the command as read-only only when every pipeline stage is a known
    inspection command. Any shell control operator fails closed to mutating; a
    bare exec with no inner command is read-only.
    """
    return kubectl_exec_rejection_reason(v_args) is None


def kubectl_exec_rejection_reason(v_args: str) -> str | None:
    """Specific reason a ``kubectl exec``/``debug`` inner command is NOT
    read-only, or ``None`` when it IS read-only."""
    return _facts_verdict(
        lambda: _facts_engine().kubectl_exec_rejection_reason_facts(v_args),
        on_error=_INTERNAL_ERROR_REASON,
    )


def is_readonly_host_command(command: str) -> bool:
    """True if a bare host command is a single read-only diagnostic.

    No UNQUOTED shell operators (pipe/redirect/chain/substitution): a host
    channel does reach a remote shell, but ``wrap_command`` quotes every
    token, so an operator arrives as a literal argument and would silently
    do nothing. Quoted literals (``'a|b'``) carry no structure and are
    admitted by the facts engine.
    """
    return host_command_rejection_reason(command) is None


def contains_shell_metachar(command: str) -> bool:
    """True if a raw command string carries a shell metacharacter.

    The same screen ``host_command_rejection_reason`` applies, exported so
    other read-only fast paths that judge at ARGV level (``host_inject``'s
    ``skip_guard`` branch) can apply it too: the argv classifier judges ONE
    command, so shell-level composition (pipe/redirect/chain/substitution)
    still needs this raw-string net. (In-program writes are no longer its
    job — ``_awk_program_mutation`` sees those at argv level since Phase 2.)
    Quoting makes a metachar a useless literal on the wire anyway, so refusing
    loses nothing.
    """
    return _facts_verdict(
        lambda: _facts_engine().contains_shell_metachar_facts(command),
        on_error=True,
    )


def host_command_rejection_reason(command: str) -> str | None:
    """Specific reason a bare host command is NOT an allowed read-only
    diagnostic, or ``None`` when it is."""
    return _facts_verdict(
        lambda: _facts_engine().host_command_rejection_reason_facts(command),
        on_error=_INTERNAL_ERROR_REASON,
    )


__all__ = [
    "is_readonly_argv",
    "is_readonly_inner_tokens",
    "readonly_inner_tokens_reason",
    "READONLY_PROBE_FIX_HINT",
    "is_readonly_kubectl_exec",
    "kubectl_exec_rejection_reason",
    "is_readonly_host_command",
    "contains_shell_metachar",
    "host_command_rejection_reason",
]
