"""Sentinel: evidence-chain cross-run rotation (自证链起点必须干净) — skills-tree guard.

Root cause this sentinel pins (surfaced by a live-fire port-occupy drill,
2026-10): the ``/tmp/*.evd`` self-proving marker chain wrote every marker
with blind append (``>>``) and NEVER rotated the evidence file at injection
start — while the sibling PID file already truncated correctly
(``echo $! > /tmp/*.pid``). Because ``/tmp`` persists across drills, a stale
evidence file left by a prior run (containing a COMPLETE
``*_INJECTED``/``*_RESTORED`` cycle) polluted the next run's chain: the
verification criterion "含 ``*_RESTORED`` 即窗口已关 / 双标记齐备 = 注入与
还原全程自证" would be falsely satisfied by the stale ``*_RESTORED``,
masking whether THIS run's recovery timer actually fired.

The invariant: within each injection payload the FIRST write to the evidence
file must TRUNCATE (``>``), symmetric with the PID file; subsequent chain
markers (execution-phase probe, timer ``*_RESTORED``) then APPEND (``>>``).
Cases whose first write is a loop/stderr append that cannot itself truncate
(e.g. inode-exhaust's ``2>>`` touch loop) rotate with an explicit
``: > <evd>`` prefix before the loop — same ``: > 状态文件`` idiom SKILL.md
already legislates for re-arm.

The sentinel converts silent drift into loud drift:

1. the k8s SKILL.md must keep the legislation anchor — the single source the
   ``.evd`` cases' rotation discipline points at; deleting it orphans the
   convention.
2. catalogue-wide zero tolerance: every case doc that writes an ``.evd``
   evidence file must carry AT LEAST ONE truncating (``>``) write to it. An
   append-only chain (zero ``>``) is the defect — cross-run residue is never
   rotated.
3. anti-vacuous baselines: the sweep must actually scan the real catalogue
   (>= _MIN_EVD_DOCS evidence-chain docs) and a known-good doc must be seen
   truncating (positive control that the detector is not vacuously passing).

Implementation note: assertions read files with ``pathlib.read_text`` +
``re`` — NOT grep-class tooling (grep silently misses these multi-KB CJK
lines). The redirect detector is WHITESPACE-ANCHORED on purpose: case
payloads are full of ``<占位符>`` whose closing ``>`` (e.g.
``<目标目录>/inode.evd``) would be mistaken for a truncate redirect by a
naive ``(?<!>)>(?!>)`` pattern — a false positive that masks a real
append-only defect. A genuine redirect operator is always preceded by
whitespace (``echo X >> f``, ``touch Y 2>>f``, ``: > f``); a placeholder's
closing ``>`` is preceded by a word/CJK char.
"""

import pathlib
import re

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_K8S_CATALOGUE = _SKILLS_DIR / "k8s-chaos-skills" / "references" / "catalogue"
_HOST_CATALOGUE = _SKILLS_DIR / "host-chaos-skills" / "references" / "catalogue"
_K8S_SKILL_MD = _SKILLS_DIR / "k8s-chaos-skills" / "SKILL.md"

# Legislation anchor the k8s SKILL.md must keep (single source for the
# evidence-chain rotation discipline).
_K8S_ANCHORS = (
    "证据文件跨轮轮转（自证链起点必须干净）",
    "首个写入必须用 `>` 截断",
)

# Evidence-file target: any path ending in ``.evd`` or the ``<evd>``
# placeholder. Redirect operator is WHITESPACE-ANCHORED (see module note) to
# avoid mistaking ``<占位符>`` closing ``>`` for a truncate; optional fd digit
# covers stderr appends (``2>>``).
_EVD_TARGET = r"(?:\S*\.evd\b|<evd>)"
_APPEND = re.compile(r"[ \t](?:\d)?>>[ \t]*" + _EVD_TARGET)
_TRUNCATE = re.compile(r"[ \t](?:\d)?>(?!>)[ \t]*" + _EVD_TARGET)

# Anti-vacuous baseline, measured at fix completion (2026-10-28): 9 catalogue
# docs write an ``.evd`` evidence chain (port-occupy, procstop x2, data-corrupt,
# diskfill, inode-exhaust, iobench, perms, dns-hijack).
_MIN_EVD_DOCS = 8

# Positive control: a known-good doc that must be seen truncating its chain.
_POSITIVE_CONTROL = "Pod_网络故障_端口被占用.md"


def _catalogue_cases() -> list[pathlib.Path]:
    cases = sorted(_K8S_CATALOGUE.rglob("*.md"))
    if _HOST_CATALOGUE.exists():
        cases += sorted(_HOST_CATALOGUE.rglob("*.md"))
    return cases


def _evd_writing_docs() -> list[tuple[pathlib.Path, int, int]]:
    """Return (path, append_count, truncate_count) for every catalogue doc
    that writes an ``.evd`` evidence file."""
    out: list[tuple[pathlib.Path, int, int]] = []
    for case in _catalogue_cases():
        text = case.read_text(encoding="utf-8")
        na = len(_APPEND.findall(text))
        nt = len(_TRUNCATE.findall(text))
        if na + nt > 0:
            out.append((case, na, nt))
    return out


def test_skill_md_rotation_anchor_present() -> None:
    """The k8s SKILL.md keeps the 证据文件跨轮轮转 legislation — the single
    source every ``.evd`` case's rotation discipline points at."""
    text = _K8S_SKILL_MD.read_text(encoding="utf-8")
    for anchor in _K8S_ANCHORS:
        assert anchor in text, f"SKILL.md 丢失证据链轮转立法锚点: {anchor!r}"


def test_every_evd_chain_rotates_at_injection_start() -> None:
    """Catalogue-wide zero tolerance: every doc writing an ``.evd`` evidence
    file must carry >= 1 truncating (``>``) write — the chain-start rotation.
    An append-only chain (zero ``>``) never rotates cross-run residue, so a
    stale ``*_RESTORED`` from a prior drill falsely satisfies the recovery /
    window-closed criterion."""
    offenders: list[str] = []
    for case, na, nt in _evd_writing_docs():
        if nt < 1:
            offenders.append(
                f"{case.relative_to(_SKILLS_DIR)} (append={na}, truncate={nt})"
            )
    assert not offenders, (
        "证据文件自证链缺起点轮转——注入载荷首个 .evd 写入必须用 `>` 截断"
        "（与 PID 文件 `echo $! > *.pid` 对称），否则跨轮残留的 *_RESTORED "
        "会假满足『含 RESTORED 即窗口已关 / 双标记齐备』判据:\n"
        + "\n".join(offenders)
    )


def test_positive_control_doc_truncates() -> None:
    """Positive control: the detector actually SEES a truncate on a known-good
    doc (guards against a vacuous pass from a broken regex/path)."""
    matches = list(_K8S_CATALOGUE.rglob(_POSITIVE_CONTROL))
    assert len(matches) == 1, f"正控文件缺失或重名: {_POSITIVE_CONTROL}"
    text = matches[0].read_text(encoding="utf-8")
    assert _TRUNCATE.search(text), (
        f"{_POSITIVE_CONTROL} 未见 .evd 截断写入——检测器可能失效（正则/路径）"
    )


def test_scan_not_vacuous() -> None:
    """The sweep baseline: the sentinel must actually be scanning the real
    catalogue's evidence-chain docs, not an empty/renamed tree."""
    docs = _evd_writing_docs()
    assert len(docs) >= _MIN_EVD_DOCS, (
        f".evd 证据链文档数 {len(docs)} 低于基线 {_MIN_EVD_DOCS}——疑似扫描失效或批量回退"
    )
