"""Structural recoverability assessment for host-level mutations.

Replaces the brittle ``_SYSTEMD_TIMER`` literal (which recognised ONLY
``systemd-run --on-active=Ns`` and silently rejected every other bounded-timer
form — the ``--on-create=600s`` a real drill actually emitted was killed here)
with a judgement by STRUCTURE and INTENT.

A host mutation is recoverable when EITHER:

  1. the operating system will undo / end it on its own — a *bounded time
     window*: a ``systemd-run`` transient timer of ANY ``--on-*`` form, a
     ``--timeout`` / ``timeout N`` self-termination, a background
     ``sleep N && <inverse>``, or an ``at`` schedule — paired with a reverse
     operation (family-specific); or
  2. the agent has already registered a rollback handle the recover graph can
     run (``execution_artifacts`` ``recovery_armed`` / ``cleanup``).

The detector never hinges on one exact flag name — any bounded-window form
counts, so the model is free to express the bound however it likes. When
recoverability cannot be confirmed, :class:`Recoverability` names EXACTLY what
is missing (a time bound, a paired inverse, or a registered rollback) so the
caller can hand the model actionable, differentiated feedback instead of a
silent hard denial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# A bounded time window, recognised by structure — not by one flag name.
# Any ``systemd-run --on-<something>=<duration-with-a-nonzero-digit>`` counts
# (``--on-active``, ``--on-calendar``, ``--on-boot``, ``--on-startup``,
# ``--on-unit-active``, and the ``--on-create`` a real drill emitted). The
# nonzero-digit tail rejects a degenerate ``=0s``.
_SYSTEMD_TIMER = re.compile(r"\bsystemd-run\b[^\n]*?--on-[a-z-]+=\S*[1-9]")
# A background delayed reversal: ``... && sleep 600 && <inverse>``.
_SLEEP_TIMER = re.compile(r"\bsleep\s+[1-9][0-9]*\b")
# A self-terminating command timeout: stress-ng ``--timeout``, ``timeout(1)``.
_SELF_TIMEOUT = re.compile(r"--timeout(?:=|\s+)[1-9][0-9]*")
_TIMEOUT_CMD = re.compile(r"\btimeout\s+[1-9][0-9]*\b")


@dataclass
class Recoverability:
    """Verdict of :func:`assess`.

    Attributes:
        recoverable: True when the operation self-limits (bound + inverse, or a
            self-terminating stressor) or a rollback handle is registered.
        missing: When not recoverable, the concrete things that are absent
            (e.g. "a time bound", "a paired inverse (iptables -D ...)"). These
            are surfaced verbatim to the model so the feedback is actionable.
    """

    recoverable: bool
    missing: tuple[str, ...] = ()


def _has_systemd_timer(lowered: str) -> bool:
    return _SYSTEMD_TIMER.search(lowered) is not None


def _has_delayed_reversal(lowered: str) -> bool:
    """A timer that will fire a *separate* inverse later (network/process/disk).

    A systemd transient timer OR a background ``sleep N && <inverse>``.
    """
    return _has_systemd_timer(lowered) or _SLEEP_TIMER.search(lowered) is not None


def _has_self_terminating_bound(lowered: str) -> bool:
    """A bound that ends the FAULT PROCESS itself (cpu/mem stressors).

    ``--timeout`` / ``timeout N`` end the stressor; a systemd transient timer
    is also accepted (parity with the pre-refactor cpu/mem contract). A bare
    ``sleep`` is intentionally NOT accepted here — it would not stop a running
    stressor.
    """
    return (
        _SELF_TIMEOUT.search(lowered) is not None
        or _TIMEOUT_CMD.search(lowered) is not None
        or _has_systemd_timer(lowered)
    )


def _iptables_rules_are_reversed(command: str) -> bool:
    """Require every inserted rule to have the same explicit delete rule."""
    mutations = re.findall(
        r"\b(ip6tables|iptables)\b\s+(?:-w(?:\s+[0-9]+)?\s+)?"
        r"(-[iad])\s+([^;&|]+)",
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
        if action in ("-i", "-a"):
            inserted.append(item)
        elif action == "-d":
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


def _network_inverse(lowered: str) -> bool:
    if re.search(r"\b(ip6tables|iptables)\b", lowered):
        return _iptables_rules_are_reversed(lowered)
    if re.search(r"(^|[\s/])tc(\s|$)", lowered):
        return re.search(r"\btc\s+qdisc\s+del\b", lowered) is not None
    if re.search(r"(^|[\s/])nft(\s|$)", lowered):
        return re.search(r"\bnft\s+(delete|flush)\b", lowered) is not None
    return False


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
        has_registered_rollback: True when a rollback handle is already on
            record (``execution_artifacts``). Short-circuits to recoverable —
            an inline timer is then unnecessary because the recover graph owns
            the undo.
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
        has_timer = _has_delayed_reversal(lowered)
        has_inverse = _network_inverse(lowered)
        return _combine(
            has_timer, has_inverse,
            inverse_hint="a paired inverse (iptables -D matching every -I/-A, "
            "or tc qdisc del / nft delete)",
        )

    if family == "process":
        # Only suspend/resume is bounded-reversible on the carrier itself.
        # Terminate-style kills (-9/-KILL/-TERM) are one-shot; recovery is the
        # workload's own restart, not a node-local timer — keep failing closed.
        has_timer = _has_delayed_reversal(lowered)
        has_stop = re.search(r"\bkill\s+(?:-stop|-sigstop|-19)\b", lowered) is not None
        has_cont = re.search(r"\bkill\s+(?:-cont|-sigcont|-18)\b", lowered) is not None
        has_inverse = has_stop and has_cont
        return _combine(
            has_timer, has_inverse,
            inverse_hint="a paired resume (kill -CONT) for the suspend (kill -STOP); "
            "terminate-style kills are one-shot and not carrier-recoverable",
        )

    if family == "disk":
        # Reclaim via truncate/fallocate (never ``rm``) targeting the SAME path.
        has_timer = _has_delayed_reversal(lowered)
        has_inverse = _disk_inverse(lowered)
        return _combine(
            has_timer, has_inverse,
            inverse_hint="a reclaim of the same fill path (truncate -s 0 <path> "
            "or fallocate -d <path>)",
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
