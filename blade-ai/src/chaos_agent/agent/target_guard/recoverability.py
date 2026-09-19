"""Structural recoverability assessment for host-level mutations.

Replaces the brittle ``_SYSTEMD_TIMER`` literal (which recognised ONLY
``systemd-run --on-active=Ns`` and silently rejected every other bounded-timer
form — the ``--on-create=600s`` a real drill actually emitted was killed here)
with a judgement by STRUCTURE and INTENT.

A host mutation is recoverable when EITHER:

  1. the operating system will undo / end it on its own — a *bounded time
     window*: a ``systemd-run`` transient timer of ANY ``--on-*`` form, a
     ``--timeout`` / ``timeout N`` self-termination, or a background
     ``sleep N && <inverse>`` — paired with a reverse operation
     (family-specific); or
  2. the CALLER asserts a rollback handle the recover graph can run (the
     ``has_registered_rollback`` seam). No production caller can make this
     assertion today — the system has no registration API for host-mutation
     rollback handles (``execution_artifacts`` ``recovery_armed`` is a
     CONSEQUENCE of an observed inline arm, not an alternative to it) — so
     the seam stays opt-in for the day such a mechanism exists; the carrier
     gate therefore always judges the inline form.

The detector never hinges on one exact flag name — any bounded-window form
counts, so the model is free to express the bound however it likes. Numeric
time bounds are sanity-capped (:data:`_MAX_BOUND_SECONDS`): a multi-month
``sleep`` is no bound at all. When recoverability cannot be confirmed,
:class:`Recoverability` names EXACTLY what is missing (a time bound, a paired
inverse, or a registered rollback) so the caller can hand the model
actionable, differentiated feedback instead of a silent hard denial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# A bounded time window, recognised by structure — not by one flag name.
# Any ``systemd-run --on-<something>=<duration-with-a-nonzero-digit>`` counts
# (``--on-active``, ``--on-calendar``, ``--on-boot``, ``--on-startup``,
# ``--on-unit-active``, and the ``--on-create`` a real drill emitted). The
# nonzero-digit tail rejects a degenerate ``=0s``. Calendar-style values
# (``*:0/10``) are intentionally not value-capped — they are recurrence
# specs, not durations.
_SYSTEMD_TIMER = re.compile(r"\bsystemd-run\b[^\n]*?--on-[a-z-]+=\S*[1-9]")
# A background delayed reversal: ``... && sleep 600 && <inverse>``.
_SLEEP_TIMER = re.compile(r"\bsleep\s+([1-9][0-9]*)\b")
# A self-terminating command timeout: stress-ng ``--timeout``, ``timeout(1)``.
_SELF_TIMEOUT = re.compile(r"--timeout(?:=|\s+)([1-9][0-9]*)")
_TIMEOUT_CMD = re.compile(r"\btimeout\s+([1-9][0-9]*)\b")

# A numeric bound beyond this is not a drill window but an unbounded fault:
# ``sleep 999999999`` formally satisfies the shape yet leaves the mutation
# in place for decades. 30 days sits far above every real drill duration.
# Applied only to the pure-number forms above — the systemd-run value is
# polyglot (``600s`` / ``15min`` / calendar) and deliberately uncapped.
_MAX_BOUND_SECONDS = 30 * 24 * 3600


@dataclass
class Recoverability:
    """Verdict of :func:`assess`.

    Attributes:
        recoverable: True when the operation self-limits (bound + inverse, or a
            self-terminating stressor) or a rollback handle is registered.
        missing: When not recoverable, the concrete things that are absent
            (e.g. "a time bound", "a paired inverse (iptables -D ...)"). These
            are surfaced verbatim to the model so the feedback is actionable.
        readonly_unproven: True when the rejection is about FORM, not about a
            missing reversal — the command carries no detectable mutation verb
            (B34: a compound ``iptables -S | grep`` probe was told to "add a
            paired iptables -D" when there was no ``-I`` to invert). Callers
            render the split-into-single-statement-probes direction instead
            of the reversal direction. Never affects ``recoverable``: the
            verdict stays fail-closed either way.
    """

    recoverable: bool
    missing: tuple[str, ...] = ()
    readonly_unproven: bool = False


def _has_systemd_timer(lowered: str) -> bool:
    return _SYSTEMD_TIMER.search(lowered) is not None


def _bounded_numeric_match(lowered: str, pattern: re.Pattern[str]) -> bool:
    """True when ANY occurrence carries a sane (non-degenerate) duration."""
    return any(int(v) <= _MAX_BOUND_SECONDS for v in pattern.findall(lowered))


def _has_delayed_reversal(lowered: str) -> bool:
    """A timer that will fire a *separate* inverse later (network/process/disk).

    A systemd transient timer OR a background ``sleep N && <inverse>``.
    """
    return (
        _has_systemd_timer(lowered)
        or _bounded_numeric_match(lowered, _SLEEP_TIMER)
    )


def _has_self_terminating_bound(lowered: str) -> bool:
    """A bound that ends the FAULT PROCESS itself (cpu/mem stressors).

    ``--timeout`` / ``timeout N`` end the stressor; a systemd transient timer
    is also accepted (parity with the pre-refactor cpu/mem contract). A bare
    ``sleep`` is intentionally NOT accepted here — it would not stop a running
    stressor.
    """
    return (
        _bounded_numeric_match(lowered, _SELF_TIMEOUT)
        or _bounded_numeric_match(lowered, _TIMEOUT_CMD)
        or _has_systemd_timer(lowered)
    )


def _iptables_rules_are_reversed(command: str) -> bool:
    """Require every inserted rule to have the same explicit delete rule.

    Long forms (``--insert`` / ``--append`` / ``--delete``) joined the short
    verbs at the B34 fix — a pure tightening: they used to be invisible to
    this regex, so ``iptables --insert INPUT 1 ...`` read as "zero rules
    inserted" and the command fell into the zero-mutation guidance below.
    """
    mutations = re.findall(
        r"\b(ip6tables|iptables)\b\s+(?:--wait(?:=[0-9]+)?\s+|-w(?:\s+[0-9]+)?\s+)?"
        r"(-[iad]|--insert|--append|--delete)\s+([^;&|]+)",
        command,
    )
    inserted: list[tuple[str, str]] = []
    deleted: list[tuple[str, str]] = []
    for binary, action, rule in mutations:
        # The rollback command may end a quoted ``sh -c`` block and be
        # followed by nohup redirection or systemd-run arguments. Neither
        # part belongs to the rule itself.
        rule = re.split(r"\s+(?:[0-9]*>|<)", rule, maxsplit=1)[0]
        normalized = " ".join(rule.strip(" \t\r\n\"'").split())
        item = (binary, normalized)
        if action in ("-i", "-a", "--insert", "--append"):
            inserted.append(item)
        elif action in ("-d", "--delete"):
            deleted.append(item)
    if not inserted:
        return False
    remaining = list(deleted)
    for item in inserted:
        if item not in remaining:
            return False
        remaining.remove(item)
    return True


def _disk_fill_path(command: str) -> str:
    """Extract the path a disk-fill command wrote to (for reclaim matching)."""
    match = re.search(r"\bof=(\S+)", command)  # dd if=... of=<path>
    if match:
        return match.group(1).strip("\"'")
    match = re.search(r"\bfallocate\s+-l\s+\S+\s+(\S+)", command)
    if match:
        return match.group(1).strip("\"'")
    return ""


# A qdisc mutation scoped to a device: ``tc qdisc add dev eth0 root ...``.
_TC_QDISC_ADD = re.compile(r"\btc\s+qdisc\s+add\b[^;&|]*?\bdev\s+(\S+)")
# Its reversal: ``tc qdisc del dev eth0 root``. An add and a del pair ONLY
# when they name the SAME device — a del on a different interface leaves the
# shaped one shaped.
_TC_QDISC_DEL = re.compile(r"\btc\s+qdisc\s+del\b[^;&|]*?\bdev\s+(\S+)")


def _tc_rules_are_reversed(lowered: str) -> bool:
    """Every device-shaped qdisc add needs a matching del on the same dev.

    Device-less adds (rare — tc almost always names a dev) fall back to the
    old existence check: any ``tc qdisc del`` counts.
    """
    added_devs = _TC_QDISC_ADD.findall(lowered)
    if not added_devs:
        return re.search(r"\btc\s+qdisc\s+del\b", lowered) is not None
    deleted_devs = _TC_QDISC_DEL.findall(lowered)
    remaining = list(deleted_devs)
    for dev in added_devs:
        if dev not in remaining:
            return False
        remaining.remove(dev)
    return True


def _network_inverse(lowered: str) -> bool:
    if re.search(r"\b(ip6tables|iptables)\b", lowered):
        return _iptables_rules_are_reversed(lowered)
    if re.search(r"(^|[\s/])tc(\s|$)", lowered):
        return _tc_rules_are_reversed(lowered)
    if re.search(r"(^|[\s/])nft(\s|$)", lowered):
        # Formal presence only — deliberately NOT paired. A true inverse is
        # ``nft delete rule ... handle N`` and the handle exists only at
        # runtime, so static text cannot verify it; ``nft flush`` is accepted
        # even though it clears the whole chain (over-broad). No skill emits
        # nft; if one ever does, this is the place to tighten.
        return re.search(r"\bnft\s+(delete|flush)\b", lowered) is not None
    return False


# Every mutation verb the network pairing frame can SEE, short and long
# forms. Anchored to the family's binaries with a gap that cannot cross a
# command separator (``;`` / ``&`` / ``|`` / newline), so a ``grep -i`` in
# the NEXT segment can never pair up with an ``iptables`` in this one —
# B34's rejected compound had exactly that shape. Judged on lowered text,
# where case cannot separate ``-X`` (delete-chain) from ``-x`` (exact
# display): the collision is accepted in the mutation direction on purpose
# (over-detecting routes guidance to the reversal frame, which stays
# fail-closed; under-detecting would hand a real mutation the read-only
# guidance). Routing-only signal — it never admits anything by itself.
_NETWORK_MUTATION_VERB = re.compile(
    r"\b(?:ip6tables|iptables)\b[^;&|\n]{0,48}?"
    r"(?:-[iadrfpze]\b|--insert\b|--append\b|--delete\b|--replace\b"
    r"|--flush\b|--delete-chain\b|--new-chain\b|--policy\b|--zero\b"
    r"|--rename-chain\b)"
    r"|\btc\b[^;&|\n]{0,48}?\b(?:add|del|delete|change|replace|mod)\b"
    r"|\bnft\b[^;&|\n]{0,48}?\b(?:add|delete|insert|flush|create|destroy)\b"
)


def _network_has_mutation_verb(lowered: str) -> bool:
    """Whether any family binary is invoked with a mutation verb (B34).

    A network-family command with NO mutation verb is not "missing a paired
    inverse" — there is nothing to invert. It reached this gate because the
    read-only probe face could not clear its compound form (redirect /
    expansion / unknown segment), so the honest guidance points at the FORM.
    """
    return _NETWORK_MUTATION_VERB.search(lowered) is not None


def _disk_inverse(lowered: str) -> bool:
    fill_path = _disk_fill_path(lowered)
    if not fill_path:
        return False
    escaped = re.escape(fill_path)
    return (
        re.search(rf"\btruncate\s+(?:-s\s*0|--size[= ]0)\b[^;&|]*{escaped}", lowered)
        is not None
        or re.search(rf"\bfallocate\s+-d\b[^;&|]*{escaped}", lowered) is not None
    )


# Terminate-style faults expressed through ``crictl stop`` — the documented
# kubectl-native equivalent of a process kill (skill case Pod_进程被杀死
# path B, task inject-e47de3e8 burned six minutes being rejected before these
# forms were recognised). A container stop leaves no persistent state — the
# kubelet rebuilds the container — so what must be bounded is the MECHANISM:
# - sustained mode: the LOOP is the durable part and is bounded twice — a
#   rounds-capped ``for`` loop with a sleep interval, and a systemd-run timer
#   whose payload terminates the loop;
# - discrete mode: a one-shot stop is a single instantaneous event — there is
#   no durable part at all, nothing to bound and nothing to undo.
# Arbitrary one-shot kills (kill -9/-TERM on host processes) keep failing
# closed: a killed host process has no kubelet to rebuild it.
_CONTAINER_STOP = re.compile(r"\bcrictl\s+stop\b")
_ROUNDS_CAPPED_LOOP = re.compile(
    r"\bfor\s+\w+\s+in\s+[^;|&\n]{1,120};\s*do\b|\bseq\s+1\s+[1-9][0-9]{0,3}\b"
)
_LOOP_INTERVAL = re.compile(r"\bsleep\s+[1-9][0-9]{0,3}\b")
# Any loop/repetition construct. A stop inside one is a SUSTAINED fault and
# must go through the double-bounded loop gate instead: a host-side loop
# outlives the agent, while one-shots are agent-paced and stop when it does.
# The head of ``for/while/until`` crosses the ``;`` before ``do``, so it is
# matched permissively (span-capped, no newline crossing); an over-match here
# only rejects, which is the safe direction.
_ANY_LOOP = re.compile(
    r"\b(?:for|while|until)\b.{0,140}?\bdo\b"
    r"|\bwatch\b"
    r"|\bseq\s+1\s+[1-9]"
)

# A port-occupation fault expressed as a LISTENER bounded by timeout(1) or
# --timeout (skill case Node_网络故障_节点端口占用): the port is held only while
# the listener runs, so ending the process IS the recovery — no inverse rule
# exists to pair. Both listener vocabularies match: ``nc -l`` and a socat
# TCP-LISTEN address (the case-law equivalent for hosts without nc). The
# EXEC:/SYSTEM:/SHELL: danger is NOT re-checked here — the family classifier
# (``carriers.classify_host_operation``) voids those shapes before this gate
# ever sees family="network", and this layer's single responsibility is the
# lifetime bound.
_PORT_LISTENER = re.compile(
    r"\bnc\b[^;&|\n]{0,20}-l\b"
    r"|\bsocat\b[^;&|\n]{0,60}\btcp[46]?-listen:"
)

# An IO-pressure burn bounded the same way (skill cases Node_磁盘IO过高,
# Pod_Terminating_Volume卸载失败): the pressure stops when the bounded burner
# dies. IO generators only — a timeout does NOT reclaim a disk FILL, so
# fallocate/truncate fills keep requiring a paired reclaim.
_IO_BURNER = re.compile(r"(^|[\s/])(dd|fio)(\s|$)")

# A cgroup-freezer suspend armed with a thaw timer (skill cases
# Pod_进程异常_进程被挂起 / Container_进程异常_Sidecar进程被挂起). The documented
# discipline is arm-then-freeze: the systemd timer whose payload writes
# THAWED to the SAME freezer.state must be registered BEFORE the FROZEN
# write — a frozen container cannot register its own rescue, and a debug pod
# may be cleaned before the thaw is due. The capture is the redirect TARGET —
# cut at any quote/shell metacharacter/whitespace so a payload quoted inside
# sh -c '...' captures the bare path — so the thaw can be paired against
# the freeze by path, not by shape alone.
_FREEZE_WRITE = re.compile(r"\becho\s+frozen\b[^;&|\n]*>\s*([^\s;&|\"']+)")
_THAW_WRITE = re.compile(r"\becho\s+thawed\b[^;&|\n]*>\s*([^\s;&|\"']+)")


def _is_bounded_container_stop_loop(lowered: str) -> bool:
    """Timer-armed, rounds-capped ``crictl stop`` loop (path-B sustained mode).

    Every requirement fails closed: no timer, no container-stop mutation, an
    uncapped loop shape, a missing interval, or a timer whose payload does not
    terminate the loop all keep the command rejected.
    """
    timer = _SYSTEMD_TIMER.search(lowered)
    if timer is None:
        return False
    if _CONTAINER_STOP.search(lowered) is None:
        return False
    if _ROUNDS_CAPPED_LOOP.search(lowered) is None:
        return False
    if _LOOP_INTERVAL.search(lowered) is None:
        return False
    # The timer must arm the loop's TERMINATOR in its own payload — a timer
    # with an unrelated payload bounds nothing. The documented terminator is
    # a pkill of the stop loop (kept outside the loop's own match pattern).
    arm_window = lowered[timer.start():timer.start() + 500]
    return re.search(r"\bpkill\b", arm_window) is not None


def _is_discrete_container_stop(lowered: str) -> bool:
    """One-shot ``crictl stop`` (path-B discrete mode).

    The fault is a single instantaneous event: the container stops and the
    kubelet recreates it within seconds — no persistent state, no fault
    window, nothing to undo. That satisfies the gate's actual purpose (no
    fault state outliving the drill) in the strongest form. Fails closed:
    no container-stop mutation, or ANY loop/repetition construct — a looped
    stop is a sustained fault whose host-side loop outlives the agent and
    must pass the double-bounded loop gate instead.
    """
    if _CONTAINER_STOP.search(lowered) is None:
        return False
    return _ANY_LOOP.search(lowered) is None


def _is_bounded_listener_or_burn(lowered: str, shape: re.Pattern[str]) -> bool:
    """A fault process that ends ITSELF at a sane bound (listener / IO burn).

    No inverse exists for these faults — the port is released, the IO
    pressure stops, the moment the bounded process dies. Both requirements
    fail closed: no sane ``timeout N`` / ``--timeout`` bound, or no matching
    fault process, keeps the command rejected.
    """
    if not (
        _bounded_numeric_match(lowered, _TIMEOUT_CMD)
        or _bounded_numeric_match(lowered, _SELF_TIMEOUT)
    ):
        return False
    return shape.search(lowered) is not None


def _is_timer_armed_freezer_suspend(lowered: str) -> bool:
    """Arm-then-freeze cgroup-freezer suspend (documented suspend form).

    Every requirement fails closed: no systemd timer, no FROZEN write to a
    freezer.state, a freeze issued BEFORE its rescue is armed, or a timer
    whose payload does not write THAWED to the SAME freezer.state all keep
    the command rejected.
    """
    timer = _SYSTEMD_TIMER.search(lowered)
    if timer is None:
        return False
    freeze = _FREEZE_WRITE.search(lowered)
    if freeze is None or "freezer.state" not in freeze.group(1):
        return False
    # Arm BEFORE freeze: a rescue registered after the freeze may never run
    # (the frozen container cannot exec; the carrier pod may be cleaned).
    if freeze.start() < timer.end():
        return False
    # The timer payload must THAW the very cgroup that was frozen — a thaw
    # aimed at another freezer.state rescues nothing. Both captures already
    # stop at quotes/metacharacters, so a payload quoted inside sh -c '...'
    # pairs against the bare outer path verbatim.
    arm_window = lowered[timer.start():timer.start() + 500]
    return any(
        thaw.group(1) == freeze.group(1)
        for thaw in _THAW_WRITE.finditer(arm_window)
    )


def assess(
    command: str,
    family: str,
    *,
    has_registered_rollback: bool = False,
) -> Recoverability:
    """Assess whether a host-level mutation self-recovers.

    Args:
        command: The host command (already unwrapped of its carrier prefix by
            the caller, or the raw string — the regexes are anchored on the
            fault binaries so either works).
        family: The fault family (``network`` / ``cpu`` / ``mem`` / ``process``
            / ``disk``) the operation belongs to.
        has_registered_rollback: Opt-in seam — True when the CALLER holds a
            rollback handle the recover graph can run and asserts it. See the
            module docstring: no production caller can assert this today (the
            system has no host-mutation rollback registration), so the gate
            always judges the inline form. Short-circuits to recoverable —
            an inline timer is then unnecessary because the recover graph
            owns the undo.
    """
    if has_registered_rollback:
        return Recoverability(True)

    lowered = command.lower()

    if family in ("cpu", "mem"):
        if _has_self_terminating_bound(lowered):
            return Recoverability(True)
        return Recoverability(
            False,
            ("a self-terminating bound (--timeout N or a systemd-run --on-* timer)",),
        )

    if family == "network":
        # A timeout-bounded listener occupies the port only while it runs:
        # ending the process IS the recovery, no inverse rule exists.
        if _is_bounded_listener_or_burn(lowered, _PORT_LISTENER):
            return Recoverability(True)
        has_timer = _has_delayed_reversal(lowered)
        has_inverse = _network_inverse(lowered)
        # B34: with no mutation verb anywhere, "add a paired iptables -D"
        # is nonsense guidance — there is no -I/-A to invert. The command
        # is either read-only inspection whose compound form the probe
        # face could not clear (redirect / expansion / unknown segment),
        # or a mutation spelled with verbs this frame cannot pair. Either
        # way the two honest directions are the ones in the guidance
        # below; both keep the rejection fail-closed.
        if not has_inverse and not _network_has_mutation_verb(lowered):
            return Recoverability(
                False,
                (
                    "a provable form: split read-only inspection into "
                    "single-statement probes (one command per exec, e.g. "
                    "`chroot /host iptables -S INPUT`), or spell a real "
                    "mutation with the standard verbs (-I/-A paired with a "
                    "matching -D) so the reversal check can verify it",
                ),
                readonly_unproven=True,
            )
        return _combine(
            has_timer, has_inverse,
            inverse_hint="a paired inverse (iptables -D matching every -I/-A, "
            "or tc qdisc del / nft delete); a port-occupation fault instead "
            "bounds its nc -l / socat TCP-LISTEN listener with timeout N so "
            "it self-terminates",
        )

    if family == "process":
        # Bounded container-stop loop: the documented kubectl-native sustained
        # process-kill (skill path B). Checked before the suspend/resume
        # pairing — a container stop needs no paired resume because it leaves
        # no persistent state; the loop IS the fault and it is double-bounded.
        if _is_bounded_container_stop_loop(lowered):
            return Recoverability(True)
        # One-shot container stop (discrete mode): an instantaneous event the
        # kubelet self-heals — no window to arm, nothing to undo.
        if _is_discrete_container_stop(lowered):
            return Recoverability(True)
        # Timer-armed cgroup-freezer suspend (documented suspend form): the
        # thaw timer is the inverse, armed BEFORE the freeze.
        if _is_timer_armed_freezer_suspend(lowered):
            return Recoverability(True)
        # Only suspend/resume is bounded-reversible on the carrier itself.
        # Terminate-style kills (-9/-KILL/-TERM) on host processes are
        # one-shot with no kubelet to rebuild them — keep failing closed.
        # Signal spellings accepted: -STOP / -SIGSTOP / -19, and the
        # ``-s|--signal <name>`` equivalents a shell may emit.
        has_timer = _has_delayed_reversal(lowered)
        has_stop = re.search(
            r"\bkill\s+(?:-s\s+(?:sig)?stop|--signal\s+(?:sig)?stop"
            r"|-sigstop|-stop|-19)\b",
            lowered,
        ) is not None
        has_cont = re.search(
            r"\bkill\s+(?:-s\s+(?:sig)?cont|--signal\s+(?:sig)?cont"
            r"|-sigcont|-cont|-18)\b",
            lowered,
        ) is not None
        has_inverse = has_stop and has_cont
        return _combine(
            has_timer, has_inverse,
            inverse_hint="a paired resume (kill -CONT) for the suspend (kill -STOP); "
            "for a cgroup-freezer suspend, arm a systemd-run --on-* timer whose "
            "payload writes THAWED to the same freezer.state BEFORE writing "
            "FROZEN; "
            "for a terminate-style fault use crictl stop instead: a one-shot "
            "stop for a discrete restart, or a rounds-capped crictl-stop loop "
            "armed with a systemd-run --on-* timer whose payload pkills the "
            "loop for a sustained fault (an arbitrary one-shot kill remains "
            "not carrier-recoverable)",
        )

    if family == "disk":
        # A timeout-bounded IO burn ends with its own process: the pressure
        # stops when the burner dies, so no reclaim pairing is needed (fills
        # do NOT qualify — see _IO_BURNER).
        if _is_bounded_listener_or_burn(lowered, _IO_BURNER):
            return Recoverability(True)
        # Reclaim via truncate/fallocate (never ``rm``) targeting the SAME path.
        has_timer = _has_delayed_reversal(lowered)
        has_inverse = _disk_inverse(lowered)
        return _combine(
            has_timer, has_inverse,
            inverse_hint="a reclaim of the same fill path (truncate -s 0 <path> "
            "or fallocate -d <path>); an IO burn loop may instead be wrapped "
            "in timeout N to self-terminate",
        )

    return Recoverability(
        False,
        ("a recognised fault family with a bounded, reversible form",),
    )


def _combine(has_timer: bool, has_inverse: bool, *, inverse_hint: str) -> Recoverability:
    if has_timer and has_inverse:
        return Recoverability(True)
    missing: list[str] = []
    if not has_timer:
        missing.append(
            "a time bound (systemd-run --on-* timer, or a background sleep N "
            "before the reversal)"
        )
    if not has_inverse:
        missing.append(inverse_hint)
    return Recoverability(False, tuple(missing))


__all__ = ["Recoverability", "assess"]
