r"""Sentinel: a host case whose INJECTION MEANS hold an exclusive ptrace attach
must carry a「判据可达性」block in its ``## 注入验证`` section.

This is the M2 (semantic-reachability) tooth. Its sibling
``test_host_case_commands_pass_guard.py`` proves the M1 axis — every prescribed
host command is ADMITTED by a guard channel (``is_readonly_host_command`` /
``ToolGuard().check``). M1 is statically decidable from command FORM. M2 is not:
a command can be perfectly admissible yet be unable to PRODUCE EVIDENCE inside
the fault window because the injector itself holds the resource the criterion
needs.

Root cause this sentinel pins (Case #16, ``Host_系统调用异常_调用延迟``,
inject-bbd983bd): the doc listed criterion #1 ``strace -T -p <pid>`` as the
UNCONDITIONAL main criterion and marked the reachable criterion #2 (app latency)
"(可选)" — a priority inversion. But both injection means hold an exclusive
ptrace attach on the target for the whole window (手段1 ``blade create strace
delay``底层 ``strace -p <pid> -f -e inject=…``; 手段2 native ``strace -p <pid>
… -T``). Linux lets a process be traced by only ONE tracer at a time, so a
SECOND ``strace -p`` attach inside the window cannot succeed — criterion #1 is
structurally unreachable exactly when it is supposed to fire. It is only
reachable AFTER ptrace is released (the 恢复验证 phase). Measured harm: the
verify agent spent a 42.7s iteration deliberating criterion #1 with a
"documented deviation" (run.log line1655-1657). The sister case
``Host_系统调用异常_调用返回错误`` has the same unreachable criterion #1
(``strace -e trace=<syscall> -p <pid>``) with NO caveat at all.

The tooth keys on the MEANS CLASS (a ptrace-exclusive tracer attach command
form), never on a case name — so a future strace/tracer-based case is caught too
(``b5c0af28``: fix the class, not the instance). The single-source authoring
contract this enforces lives at
``skills/host-chaos-skills/references/criterion-reachability.md``.

Section extraction reuses the authoritative single source
``chaos_agent.utils.skill_case_section.find_section`` — the sentinel does NOT
reinvent section boundaries, and because that extractor masks fenced code, the
「判据可达性」caveat must live as PROSE (a ``>`` blockquote) inside
``## 注入验证`` to be seen (matching the contract's 落位形态 rule).
"""

from __future__ import annotations

import pathlib
import re

import pytest

from chaos_agent.utils.skill_case_section import find_section

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_HOST_CATALOGUE = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"

#: Means-class signature: the doc prescribes a ptrace-EXCLUSIVE tracer attach as
#: an injection means. Two forms, keyed on COMMAND SHAPE (not case name):
#:   手段1 — ChaosBlade strace executor: ``blade create strace <delay|error> …``
#:           (底层 ``strace -p <pid> -f -e inject=…`` holds ptrace for the window)
#:   手段2 — native strace ptrace attach: ``strace … -p <pid> …``
#: A process can be traced by only ONE tracer at a time, so any case arming one
#: of these makes a SECOND ``strace -p`` criterion unreachable inside the window.
_PTRACE_TRACER_MEANS_RE = re.compile(
    r"blade\s+create\s+strace\b"        # 手段1: ChaosBlade strace injector
    r"|\bstrace\b[^\n]*?\s-p\b",        # 手段2: native strace ptrace attach
    re.IGNORECASE,
)

#: The 判据可达性 caveat marker the contract mandates inside ``## 注入验证``.
#: Keyed on the BLOCKQUOTE form (``> 判据可达性…`` / ``> **判据可达性（…）**：``)
#: per the contract's §四 落位形态 — NOT a bare substring. A bare
#: ``"判据可达性" in section`` check is too weak: a doc that LOST its caveat
#: blockquote but kept a cross-reference phrase like「见上方判据可达性」in a
#: 判定规则 bullet would still pass (mutation-verified: this exact vacuity was
#: caught by the remove-the-caveat tooth check). Anchoring to a ``>``-prefixed
#: line makes the tooth bite the actual caveat, not a mention of it.
_REACHABILITY_BLOCKQUOTE_RE = re.compile(
    r"^[ \t]*>[ \t]*(?:\*\*)?判据可达性", re.MULTILINE
)

#: Anti-vacuous baseline (measured 2026-10): exactly these two syscall-anomaly
#: docs use a ptrace-exclusive tracer means. They MUST be in the hit set — if
#: the extractor silently stops matching them the tooth would go vacuous (the
#: failure mode ``b5c6b377`` names). The tooth itself is "EVERY hit doc carries
#: the caveat", NOT "hit set == these two" (that would be a case-name branch).
_KNOWN_PTRACE_MEANS_CASES = (
    "Host_系统调用异常_调用延迟.md",
    "Host_系统调用异常_调用返回错误.md",
)


def _host_case_docs() -> list[pathlib.Path]:
    return sorted(_HOST_CATALOGUE.rglob("*.md"))


def _uses_ptrace_exclusive_tracer(text: str) -> bool:
    """True if the doc prescribes a ptrace-exclusive tracer attach as a means."""
    return _PTRACE_TRACER_MEANS_RE.search(text) is not None


def _ptrace_means_cases() -> list[tuple[str, str]]:
    """``(case_relpath, 注入验证_section_text)`` for every ptrace-means case.

    The 注入验证 body comes from the authoritative single-source extractor; a
    doc with no such section yields ``""`` (the assertion then fails loudly —
    a ptrace-means case with no 注入验证 section is itself a defect).
    """
    out: list[tuple[str, str]] = []
    for path in _host_case_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _uses_ptrace_exclusive_tracer(text):
            continue
        rel = str(path.relative_to(_SKILLS_DIR))
        section = find_section(text, "注入验证")
        out.append((rel, section if section is not None else ""))
    return out


_PTRACE_CASES = _ptrace_means_cases()


def test_ptrace_means_corpus_is_not_empty() -> None:
    """Guards the extractor: the known ptrace-means cases MUST be detected, else
    every assertion below is vacuous."""
    hit_names = {pathlib.Path(rel).name for rel, _ in _PTRACE_CASES}
    missing = set(_KNOWN_PTRACE_MEANS_CASES) - hit_names
    assert not missing, (
        f"means-signature extractor missed known ptrace-tracer cases: {sorted(missing)} "
        f"(detected: {sorted(hit_names)}) — the regex or the corpus changed shape"
    )


@pytest.mark.parametrize(
    "case,section",
    _PTRACE_CASES,
    ids=[rel for rel, _ in _PTRACE_CASES],
)
def test_ptrace_means_case_pins_criterion_reachability(case: str, section: str) -> None:
    assert _REACHABILITY_BLOCKQUOTE_RE.search(section), (
        f"{case} uses a ptrace-EXCLUSIVE tracer as an injection means, so a "
        f"second `strace -p <pid>` verification criterion is UNREACHABLE inside "
        f"the fault window (only one tracer may attach a process at a time). Its "
        f"`## 注入验证` section therefore MUST carry a 「判据可达性」 "
        f"BLOCKQUOTE (`> 判据可达性…`) that (1) names the ptrace-exclusivity trap "
        f"and demotes the window-unreachable criterion, and (2) gives the "
        f"reachable alternative (app end-to-end behaviour / white-box "
        f"`pgrep -a strace` + `/proc/<pid>/status` TracerPid). A bare mention of "
        f"the phrase in prose does NOT satisfy this — the caveat must be the "
        f"blockquote. See "
        f"skills/host-chaos-skills/references/criterion-reachability.md. "
        f"This is the Case #16 priority-inversion defect (criterion #1 "
        f"`strace -T -p` main-listed yet unreachable, reachable #2 marked 可选)."
    )
