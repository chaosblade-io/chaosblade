r"""Every host command written in a host skill case must survive a guard channel.

The kubectl-face twin (``test_skill_case_commands_pass_guard.py``) proves every
``kubectl`` line in the corpus is admitted by ``ToolGuard``. The HOST face had
no such tooth — and that gap is how Case #12 (``Host_进程异常_进程被杀死``)
shipped form A's synchronous ``systemd-run --unit=blade-killloop-… sh -c '…
systemctl kill …'`` kill-loop: a synchronous ``systemd-run`` is an
arbitrary-execution bypass (``_check_systemd_run`` admits ONLY the
``--on-active`` timer form), and ``systemctl kill`` is not in
``SYSTEMCTL_ALLOWED_SUBCOMMANDS`` — so both the carrier AND its payload were
guard-unreachable. This file closes that axis: a host command the guard refuses
on BOTH channels makes the drill unexecutable by construction.

Neither is caught by ``test_skill_machinery_matrix_sentinel`` (that sentinel
judges ATTRIBUTION — machinery vs fault — a different axis) nor by the kubectl
tooth (kubectl-only).

The invariant is POSITIVE membership, not negative exclusion (a ``not X``
assertion would silently pass any equivalent un-whitelisted form): every
prescribed host command whose head is a whitelisted-candidate host binary must
be admitted by ``is_readonly_host_command`` (the host_read observation channel)
OR ``ToolGuard().check`` (the host_inject mutation channel). A command admitted
by neither is a case/guard drift.

Declared head boundary — ``pkill`` / ``killall`` are NOT judged heads. They are
deliberately absent from the binary whitelist (a name-based kill is an
arbitrary-process-kill risk; the guard forces a numeric-PID ``kill``), and in
this corpus they appear ONLY inside ``恢复命令`` manual-teardown blocks — the
human-run fallback that stops a stressor or a Tier-2 ``setsid`` carrier, which
rides no agent dispatch channel. Judging them here would false-positive on
live-verified cases, so a case that prescribed ``pkill`` as an AGENT-sent
injection is instead caught by review + the guard's own refusal, not this tooth.

Scope, declared (same discipline as the kubectl twin — never silently vacuous):
  - only fenced-code-block lines whose head is a host binary are judged; inline
    prose (where the cases list FORBIDDEN forms like ``pkill`` / ``kill -9
    <name>`` as negative examples) is not a prescription and is not scanned;
  - ``#``-prefixed fenced lines are skipped — a leading ``#`` is a shell
    comment, so a step annotation or a commented-out reference form runs
    nothing and prescribes nothing (this also dodges the artifact where a
    ``\``-terminated comment line is textually joined to the next line);
  - a multi-word ``<… …>`` prose template (e.g. ``<应用启动命令 或 systemctl
    start <service>>``) describes what to put, not a literal runnable command,
    so a segment still carrying one after materialization is skipped;
  - top-level ``;`` / ``&&`` sequences (human shorthand for "run these
    separately") are split quote-aware and each segment judged — a ``;`` INSIDE
    a quoted ``sh -c '…'`` carrier payload is part of that payload (the guard
    re-admits it via ``_admit_script``), never a split point;
  - redirect discard-sinks (``2>/dev/null``, ``>/dev/null``, ``2>&1``) are
    stripped before judging — they are glue the exec-form guard does not model;
  - placeholders are materialized by TYPE (PID operands numeric, timer/duration
    values numeric) so the command has the shape the agent actually sends.
"""

import pathlib
import re
import shlex

import pytest

from chaos_agent.tools.guard import ToolGuard
from chaos_agent.tools.readonly import is_readonly_host_command

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_HOST_CATALOGUE = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"

#: Head binaries whose fenced lines are host prescriptions. ``kubectl`` is
#: judged by the kubectl-face twin; ``chroot``/``nsenter``/``unshare`` are
#: host-ENTRY wrappers (a different delivery shape the exec-gate models, not a
#: bare host_inject command), so both families are out of this tooth's scope.
_HOST_HEADS = frozenset({
    "kill", "systemctl", "systemd-run", "setsid", "ps", "ss", "pgrep", "pidof",
    "nc", "socat", "dd", "stress-ng", "stress", "iptables", "ip6tables", "nft",
    "tc", "fuser", "strace", "truncate", "chmod", "cp", "mv", "rm", "date",
    "timedatectl", "chronyc", "ntpdate", "ping", "df", "du", "sleep",
    "fallocate", "fio", "blade", "journalctl", "cat", "ls", "echo", "free",
    "dmsetup", "mount", "umount",
})

#: Discard-sink redirect tokens stripped before judging (exec-form glue).
_REDIRECT_TOKENS = ("2>/dev/null", ">/dev/null", "2>&1", ">/dev/null 2>&1")

_PLACEHOLDER_RE = re.compile(r"<[^<>\s]+>")


def _materialize(cmd: str) -> str:
    """Substitute ``<…>`` placeholders with TYPE-appropriate literal values so
    the command has the shape the agent sends (a numeric-PID ``kill``, a numeric
    ``--on-active`` timer, a numeric port filter). Mirrors the kubectl twin's
    ``sleep PLACEHOLDER`` → ``sleep 600`` normalisation, generalised by type."""
    def sub(match: re.Match) -> str:
        name = match.group(0)[1:-1].lower()
        if "pid" in name:                       # <pid>, <MainPID>, <stress-ng-pid>
            return "4242"
        if any(k in name for k in ("second", "duration", "delay", "timeout")):
            return "600"
        if "percent" in name:
            return "80"
        if "count" in name:
            return "100"
        if "port" in name:
            return "8080"
        if "offset" in name:
            return "30"
        if any(k in name for k in ("分配量", "填充量", "mb数", "size", "大小")):
            return "500"
        if any(k in name for k in ("权限", "mode", "octal")):
            return "644"
        return "PLACEHOLDER"                    # names / paths / uids / ips / domains
    return _PLACEHOLDER_RE.sub(sub, cmd)


def _split_top_level(cmd: str) -> list[str]:
    """Split on ``;`` / ``&&`` that sit OUTSIDE quotes. A separator inside a
    quoted ``sh -c '…'`` carrier payload is part of that payload (the guard
    re-admits it), never a split point."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if cmd[i:i + 2] == "&&":
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _strip_redirects(argv: list[str]) -> list[str]:
    """Drop tokens that are EXACTLY a discard-sink redirect (top-level glue like
    ``systemctl reset-failed X 2>/dev/null``). A redirect EMBEDDED inside a
    larger token is part of a quoted ``sh -c`` payload (``exec >/dev/null
    2>&1; …``) the guard must see intact to admit the carrier — never touch it."""
    return [tok for tok in argv if tok not in _REDIRECT_TOKENS]


#: A multi-word ``<… …>`` span (spaces inside) is a PROSE TEMPLATE describing
#: what to put ("应用启动命令 或 systemctl start <service>"), not a literal
#: runnable command — the single-token ``_PLACEHOLDER_RE`` deliberately does not
#: match it. A segment still carrying one after materialization is a template,
#: not a prescription, so it is skipped (declared boundary, mirroring the kubectl
#: twin's prose exclusion); the carrier STRUCTURE it sits in is validated by the
#: sibling segments and by the concrete-payload forms elsewhere in the corpus.
_PROSE_TEMPLATE_RE = re.compile(r"<[^<>]*\s[^<>]*>")


def _host_command_segments() -> list[tuple[str, str]]:
    """``(case_relpath, materialized_segment)`` for every judged host command.

    Fenced-block lines only; head must be a host binary; backslash continuations
    joined; placeholders materialized; top-level ``;``/``&&`` split; redirect
    sinks stripped. A segment that does not shlex-parse (unbalanced quotes = a
    prose fragment) is skipped.
    """
    out: list[tuple[str, str]] = []
    for path in sorted(_HOST_CATALOGUE.rglob("*.md")):
        rel = str(path.relative_to(_SKILLS_DIR))
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"\\\s*\n\s*", " ", text)  # join line continuations
        in_fence = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
            if stripped.startswith("#"):
                # A comment — either a Chinese step annotation or a
                # commented-out reference form. In shell a leading ``#`` runs
                # nothing, so neither is a live prescription (same boundary the
                # kubectl twin draws around prose). Skipping also avoids the
                # artifact where a ``\``-terminated COMMENT line is joined to
                # the next line (a backslash inside a comment does not continue
                # it in shell), leaving an interior ``#`` the guard would misread
                # as the payload binary.
                continue
            cmd = stripped
            cmd = re.sub(r"^\d+\.\s*", "", cmd).strip("`- ")
            head = cmd.split()[0] if cmd.split() else ""
            if head not in _HOST_HEADS:
                continue
            cmd = _materialize(cmd)
            for seg in _split_top_level(cmd):
                if _PROSE_TEMPLATE_RE.search(seg):
                    continue  # a prose template, not a runnable prescription
                seg_head = seg.split()[0] if seg.split() else ""
                if seg_head not in _HOST_HEADS:
                    continue  # a non-command tail (e.g. a bare redirect fragment)
                out.append((rel, seg))
    return out


_SEGMENTS = _host_command_segments()


def _admitted(cmd: str) -> tuple[bool, str]:
    """True if the command is admitted on EITHER host channel."""
    argv = _strip_redirects(shlex.split(cmd))
    if not argv:
        return True, "empty after redirect-strip"
    if is_readonly_host_command(cmd):
        return True, "host_read (readonly)"
    allowed, reason = ToolGuard().check(argv)
    return allowed, reason


def test_host_corpus_is_not_empty():
    """Guards the extractor: a silently empty corpus would make every assertion
    below vacuous (the failure mode ``b5c6b377`` names)."""
    assert len(_SEGMENTS) > 80, (
        f"only {len(_SEGMENTS)} host command segments extracted from "
        f"{_HOST_CATALOGUE} — the extractor or the host catalogue changed shape"
    )


@pytest.mark.parametrize(
    "case,cmd",
    _SEGMENTS,
    ids=[f"{c}::{m[:40]}" for c, m in _SEGMENTS],
)
def test_host_case_command_passes_a_guard_channel(case, cmd):
    try:
        shlex.split(cmd)
    except ValueError:
        pytest.skip("unbalanced quotes — a prose fragment, not a runnable command")
    allowed, reason = _admitted(cmd)
    assert allowed, (
        f"{case} prescribes a host command NEITHER guard channel admits:\n"
        f"  {cmd}\n  → {reason}\n"
        "host_read rejects it as non-readonly AND host_inject's ToolGuard "
        "refuses it, so the drill step is unexecutable by construction. Either "
        "admit the form (a real drill need the whitelist is missing) or fix the "
        "case to a reachable form. This is the tooth Case #12's synchronous "
        "systemd-run kill-loop carrier and its `systemctl kill` payload would "
        "have tripped."
    )
