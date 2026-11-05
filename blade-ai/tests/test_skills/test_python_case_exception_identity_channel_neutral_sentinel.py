r"""Sentinel: a python-app case whose injection means is ``throwCustomException``
must state its exception-identity criterion CHANNEL-NEUTRALLY.

``blade create python <lib> throwCustomException`` only guarantees the configured
exception is RAISED in-process. HOW (or whether) that exception becomes observable
is entirely the TARGET APP's own error-handling design: an app that does
``except Exception: return 500`` surfaces the type/message in the HTTP error
response body and NEVER writes a stack trace to its log; an app that does
``logger.exception()`` surfaces it in the log. The mechanism does not dictate the
channel, so a case that hardcodes 「查看应用日志」 as the sole channel for
confirming the exception type/message prescribes an evidence path the mechanism
does not guarantee.

Static defect this sentinel pins: ``throwCustomException`` only guarantees the
in-process raise; it does NOT guarantee the exception lands in the log. The case
docs nonetheless hardcoded 「查看应用日志」 (Step 2) and 「确认应用日志可见异常堆栈」
(precondition #4) as the channel for confirming exception identity — prescribing
an evidence path the mechanism does not guarantee, and self-inconsistent with the
docs' own 故障现象 (which names the observable as 「接口返回 5xx 或降级响应」). For
the common HTTP-app shape (``except → 500 body``, no logging) that instruction is
literally unachievable. This is a STATIC doc-accuracy defect (mechanism semantics
+ grep-confirmed self-inconsistency), fixable directly per ``385b1a3e`` tier-1.

NOT a verdict-determinative fix — honest scope (corrected 2026-11-03 after
re-reading the artifact, per ``f07404b6``): a live drill
(Python_MySQL异常_数据库查询抛异常, 2026-10-31, inject-4a64856b) returned
``overall=partial``, but the partial was caused SOLELY by an environment-specific
app wedge (single-threaded ``http.server`` + connection-pool leak on the exception
path → all routes unreachable in-phase), NOT by the log-bound criterion. The LLM
verifier already reasoned channel-neutrally (it framed Step 2 as "a 5xx body OR
log line"), and the FIRST drill of the same case reached ``verified`` under the
OLD log-bound doc. So this sentinel guards a STATIC doc invariant
(channel-neutrality), not a partial→verified behavioural fix. The gap is a shared
VERBATIM 判据 pattern across the whole ``throwCustomException`` family
(mysql/redis/kafka/http 异常), so the fix is class-wide, not a single-case patch
(``b5c0af28``).

The tooth keys on the MEANS CLASS (the ``throwCustomException`` command shape),
never on a case name — so a future python-exception case is caught too. The
single-source authoring contract this enforces: the exception-identity criterion
MUST be channel-neutral (admit log line OR error-response body OR trace) and MUST
NOT prescribe 「应用日志」 as the exclusive channel.

Section extraction reuses the authoritative single source
``chaos_agent.utils.skill_case_section.find_section`` — the sentinel does NOT
reinvent section boundaries. Because that extractor masks fenced code, the
channel-neutral markers must live as PROSE (numbered-list items) inside the
sections to be seen.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from chaos_agent.utils.skill_case_section import find_section

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_PY_CATALOGUE = _SKILLS_DIR / "python-app-chaos-skills" / "references" / "catalogue"

#: Means-class signature: the doc prescribes an in-process ``throwCustomException``
#: injection. Keyed on COMMAND SHAPE (not case name): the exception this action
#: raises is surfaced through whatever channel the target app's error handling
#: exposes, which the mechanism does not dictate.
_THROW_CUSTOM_EXCEPTION_RE = re.compile(
    r"blade\s+create\s+python\s+\w+\s+throwCustomException",
    re.IGNORECASE,
)

#: Canonical channel-neutral markers the fix legislates. Both live in prose.
#:   注入验证 — the exception-identity criterion is confirmed via the app's actual channel
#:   资源准备 — the precondition admits an observable channel beyond logs
_VERIFY_MARKER = "经应用实际暴露的通道"
_PRECONDITION_MARKER = "可观测通道"

#: The over-specific sole-channel forms that MUST be gone once neutralized.
_VERIFY_OLD_FORM = "查看应用日志确认"
_PRECONDITION_OLD_FORM = "确认应用日志可见异常堆栈"

#: Anti-vacuous baseline (measured 2026-11): exactly these four exception cases
#: use a ``throwCustomException`` means. They MUST be in the hit set — if the
#: extractor silently stops matching them the tooth would go vacuous. The tooth
#: itself is "EVERY hit doc is channel-neutral", NOT "hit set == these four"
#: (that would be a case-name branch).
_KNOWN_THROW_EXCEPTION_CASES = (
    "Python_MySQL异常_数据库查询抛异常.md",
    "Python_Redis异常_缓存不可用.md",
    "Python_Kafka异常_消息生产失败.md",
    "Python_HTTP异常_下游接口连接失败.md",
)


def _python_case_docs() -> list[pathlib.Path]:
    return sorted(_PY_CATALOGUE.rglob("*.md"))


def _uses_throw_custom_exception(text: str) -> bool:
    """True if the doc prescribes an in-process ``throwCustomException`` means."""
    return _THROW_CUSTOM_EXCEPTION_RE.search(text) is not None


def _throw_exception_cases() -> list[tuple[str, str, str]]:
    """``(case_relpath, 注入验证_section, 资源准备_section)`` per throwCustomException case.

    Section bodies come from the authoritative single-source extractor; a doc
    missing a section yields ``""`` (the assertion then fails loudly — a
    throwCustomException case with no 注入验证 / 资源准备 section is itself a
    defect).
    """
    out: list[tuple[str, str, str]] = []
    for path in _python_case_docs():
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _uses_throw_custom_exception(text):
            continue
        rel = str(path.relative_to(_SKILLS_DIR))
        verify = find_section(text, "注入验证")
        precond = find_section(text, "资源准备")
        out.append((
            rel,
            verify if verify is not None else "",
            precond if precond is not None else "",
        ))
    return out


_THROW_CASES = _throw_exception_cases()


def test_throw_exception_corpus_is_not_empty() -> None:
    """Guards the extractor: the known throwCustomException cases MUST be
    detected, else every assertion below is vacuous."""
    hit_names = {pathlib.Path(rel).name for rel, _, _ in _THROW_CASES}
    missing = set(_KNOWN_THROW_EXCEPTION_CASES) - hit_names
    assert not missing, (
        f"means-signature extractor missed known throwCustomException cases: "
        f"{sorted(missing)} (detected: {sorted(hit_names)}) — the regex or the "
        f"catalogue changed shape"
    )


@pytest.mark.parametrize(
    "case,verify,_precond",
    _THROW_CASES,
    ids=[rel for rel, _, _ in _THROW_CASES],
)
def test_verify_criterion_is_channel_neutral(case: str, verify: str, _precond: str) -> None:
    assert verify, f"{case} has no 注入验证 section — a throwCustomException case must state one"
    assert _VERIFY_OLD_FORM not in verify, (
        f"{case} 注入验证 still prescribes 「{_VERIFY_OLD_FORM}…」 — the exception "
        f"type/message is surfaced through whatever channel the target app's "
        f"error handling exposes (an app that catches → returns 5xx never logs a "
        f"stack trace), so 应用日志 MUST NOT be the exclusive channel. This is a "
        f"STATIC doc-accuracy defect (the mechanism does not guarantee the log "
        f"channel), not a verdict-determinative fix — see module docstring."
    )
    assert _VERIFY_MARKER in verify, (
        f"{case} 注入验证 lacks the channel-neutral marker 「{_VERIFY_MARKER}」 — "
        f"the exception-identity criterion must admit the app's actual observable "
        f"channel (log line OR error-response body OR trace), not hardcode 应用日志."
    )


@pytest.mark.parametrize(
    "case,_verify,precond",
    _THROW_CASES,
    ids=[rel for rel, _, _ in _THROW_CASES],
)
def test_precondition_is_channel_neutral(case: str, _verify: str, precond: str) -> None:
    assert precond, f"{case} has no 资源准备 section — a throwCustomException case must state one"
    assert _PRECONDITION_OLD_FORM not in precond, (
        f"{case} 资源准备 still states the bare precondition 「{_PRECONDITION_OLD_FORM}」 "
        f"— log visibility is not guaranteed by the mechanism (it depends on the "
        f"app's own except handling), so the precondition must admit any observable "
        f"channel rather than assume a stack trace lands in the log."
    )
    assert _PRECONDITION_MARKER in precond, (
        f"{case} 资源准备 lacks the channel-neutral marker 「{_PRECONDITION_MARKER}」 — "
        f"the precondition must confirm the app surfaces the caught exception via "
        f"SOME observable channel (log stack OR error-response body OR trace)."
    )
