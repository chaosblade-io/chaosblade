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
     ``kubectl config`` read-only. Each ``_check_*`` returns a full
     ``GuardFeedback`` (or ``None`` to pass) so it owns every field of its
     own verdict instead of the caller flattening it into a string.
  3. Token-level checks on ``ParsedCommand.host_relevant_tokens()``
     only — no more ``" ".join(cmd)`` cross-token false positives.
     Two checks per host token:
       a. Solo shell-metachar (``SUSPICIOUS_SOLO_TOKENS``) — ``|``
          ``;`` ``&`` ``>`` ``<`` etc.
       b. Regex blacklist (``PARAM_BLACKLIST_PATTERNS``).
     Data payload flag values (``-p`` ``--patch`` ``--from-literal``
     ``-l`` ``--field-selector`` …) and container_command (after
     ``--`` for ``kubectl exec/run/attach/debug``) are excluded from
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
from pathlib import Path

from chaos_agent.models.command_result import CommandResult
from chaos_agent.tools.guard_feedback import GuardFeedback, ViolatedConstraint
from chaos_agent.tools.guard_parser import (
    SUSPICIOUS_SOLO_TOKENS,
    parse_command,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# Device-node families whose direct write = disk destruction. Covers raw disks
# (sd/nvme/vd/hd/xvd), LVM & device-mapper (dm-/mapper/), software RAID (md),
# eMMC (mmcblk), loopback (loop), optical (sr), mainframe DASD (dasd), network
# block devices (nbd) and Ceph RBD (rbd).
# NOTE: modern servers usually root on LVM (/dev/mapper/...), so omitting these
# families would leave the most common layout unprotected.
_BLOCK_DEVICE_FAMILIES = r"sd|nvme|vd|hd|xvd|disk|dm-|md|mmcblk|loop|dasd|sr|nbd|rbd|mapper/"


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


class ToolGuard:
    """Security guard for tool command execution."""

    # Guard-owned base binaries: read-only diagnostics / transport primitives
    # that belong to no single fault backend. Every other admitted binary is
    # contributed by a provider's ``injection_binaries`` (see
    # ``_default_allowed_commands``) — this keeps "which binary a backend runs"
    # as knowledge owned by that backend.
    BASE_COMMANDS = {
        # Diagnostics / transport
        "df", "ping", "sleep",
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
        "blade", "kubectl", "wiz",
        # Host fault injection (host_shell)
        "iptables", "ip6tables", "nft", "tc",
        "stress", "stress-ng", "dd", "fallocate", "fio",
        # Diagnostics (guard base)
        "df", "ping", "sleep",
        # Host recovery / low-risk fault primitives (Tier 1, host_shell): bounded
        # blast radius, reversible or self-limiting, single-command form.
        "truncate", "chmod", "cp", "kill", "ntpdate", "chronyc",
        # Host service / time control (Tier 2, host_shell): admitted only WITH
        # the extra per-binary guards below (systemctl verb whitelist, kill PID /
        # chmod recursion checks). Never admit interpreters or shell (sh/bash/
        # python).
        "systemctl", "date", "timedatectl", "mv",
        # Single-resource fault primitives (Tier 2, host_shell): each is the only
        # single-command way to express its fault, and each is narrowed to that
        # form by its own guard (_check_nc listen-only, _check_fuser
        # port-spec-only, _check_strace attach-only).
        "nc", "fuser", "strace",
    }

    # kubectl subcommands the tool layer will RUN (Gate ②). This is the
    # execution gate, deliberately narrower than
    # ``classifier.DESTRUCTIVE_KUBECTL_SUBS`` (a safety-classification set that
    # also recognises verbs we refuse to run, e.g. edit/replace/run/proxy).
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
    # they exceed the blast radius of a single-service drill.
    SYSTEMCTL_ALLOWED_SUBCOMMANDS = {
        "start", "stop", "restart", "mask", "unmask", "status",
        "is-active", "is-enabled",
    }

    PARAM_BLACKLIST_PATTERNS = [
        r"rm\s+-rf",
        r">\s*/dev/",
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
        self.kubectl_subcommands = kubectl_subcommands or self.KUBECTL_ALLOWED_SUBCOMMANDS
        self.systemctl_subcommands = (
            systemctl_subcommands or self.SYSTEMCTL_ALLOWED_SUBCOMMANDS
        )
        self.param_blacklist = param_blacklist or self.PARAM_BLACKLIST_PATTERNS
        self._compiled_patterns = [re.compile(p) for p in self.param_blacklist]

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
                config_action = cmd[config_index + 1] if config_index + 1 < len(cmd) else ""
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

        # 3b. Per-binary host guards for the Tier-2 / signal binaries. These
        # narrow an admitted binary down to its safe, single-target forms. Each
        # returns a full GuardFeedback so the offending token and the compliant
        # form land in their own fields rather than being concatenated into one
        # opaque sentence.
        for guarded, checker in (
            ("systemctl", self._check_systemctl),
            ("kill", self._check_kill),
            ("chmod", self._check_chmod),
            ("nc", self._check_nc),
            ("fuser", self._check_fuser),
            ("strace", self._check_strace),
        ):
            if binary == guarded:
                feedback = checker(cmd)
                if feedback is not None:
                    return feedback
                break

        # 4 + 5. Token-level checks (SUSPICIOUS_SOLO_TOKENS + regex blacklist)
        # on host-relevant tokens only. Excludes data_payload_values and
        # container_command (tokens after ``--`` for exec/run/attach/debug) —
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
                            "Shell pipe '|' is not supported (exec-form, "
                            "shell=False)."
                        ),
                        offending=token,
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
                    compliant_form=(
                        "Express the intent as a single standalone command."
                    ),
                )
            for pattern in self._compiled_patterns:
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
        Returning the full :class:`GuardFeedback` (rather than a
        ``(ok, reason)`` pair like the other per-binary checks) is what lets the
        per-flag cause go in ``reason`` and the alternative in
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
            arg in ("-l", "-lk", "-kl", "--listen") or (
                arg.startswith("-") and not arg.startswith("--")
                and "l" in arg[1:] and arg[1:].isalpha()
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
        if not any(a == "-k" or (
            a.startswith("-") and not a.startswith("--") and "k" in a[1:]
        ) for a in cmd[1:]):
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
                        "resolved here (exec-form runs no shell). "
                        + resolve_first
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

    def audit_log(
        self,
        cmd: list[str],
        result: CommandResult,
        task_id: str = "",
    ) -> None:
        """Record an execution audit log entry."""
        log_entry = {
            "timestamp": now_iso(),
            "task_id": task_id,
            "command": cmd,
            "exit_code": result.exit_code,
            "duration_ms": round(result.duration_ms, 1),
        }
        logger.info(json.dumps(log_entry, ensure_ascii=False))
