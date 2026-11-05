"""Sentinel (family-level): the blast-radius restart-drift criterion must be
DELTA-FROM-EACH-POD'S-OWN-BASELINE across the WHOLE k8s catalogue — not a naive
absolute "same-node pods match the baseline list (no abnormal restart)" — and the
general discipline must be single-sourced in SKILL.md.

This propagates the #10-R2 W-10R-2 root fix (see
``test_case_diskio_blast_radius_delta_sentinel.py`` for the disk-IO-specific
anchor) to the sibling cases that shared the identical naive wording.

Root cause this sentinel pins: a production node's baseline is NEVER clean — it
routinely carries pre-existing abnormal pods (CrashLoopBackOff restarting on its
own cadence, long-stuck Pending/Terminating). A case doc that frames the
blast-radius cross-check as an ABSOLUTE state ("同节点 Pod 与基线清单一致（无
Evicted、无异常重启）" / "无驱逐/无重启漂移即爆炸半径未失控") will read a
pre-existing pod's self-drift during the fault window as injection-caused loss of
control. The correct criterion is per-pod DELTA against each pod's OWN baseline,
with pre-existing abnormal pods explicitly locked at planning time.

This is a doc/code inconsistency, not a code gap: the framework's deterministic
side-effect layer (``ContainerRestartDetector``) ALREADY computes
``delta = current_rc - baseline_rc``, filters restarts preceding injection, and
peels exit-0 natural completions. The absolute wording contradicts the framework
and forces every run to re-derive the delta judgment (d4ac1868 type-a).

All of this is stable universal fact (production nodes carry pre-existing abnormal
pods; the framework detector is delta-based), NOT cluster-relative — so per
85a04a51 it belongs in the docs / SKILL.md.

Scope note: this sentinel guards the CRITERION WORDING family-wide (the
unambiguous W-10R-2 invariant). It deliberately does NOT assert global absence of
``-o wide`` — that form has legitimate non-blast-radius uses (e.g. enumerating
preemption candidates in Pod_被驱逐重建, listing node pods in Node_网络故障). The
truncation-safe census rewrite is applied within the sibling blast-radius sections
but is not globally enforced here to avoid false positives.
"""

from __future__ import annotations

import pathlib

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_K8S = _SKILLS_DIR / "k8s-chaos-skills"
_CATALOGUE = _K8S / "references" / "catalogue"
_SKILL_MD = _K8S / "SKILL.md"

# Naive ABSOLUTE blast-radius drift wordings. Each is a standalone "any drift /
# mismatch vs the baseline list = loss of control" phrasing that ignores
# pre-existing self-drifting pods. The delta rewrite never reproduces these exact
# substrings (it inserts "注入新增的" / "相对自身基线" between 无 and Evicted/重启).
_NAIVE_ABSOLUTE_PATTERNS = (
    "与基线清单一致（无 Evicted、无异常重启）",
    "与基线清单一致（无 Evicted、无异常",  # line-wrapped variant (进程被挂起)
    "无异常重启/无漂移即爆炸半径未失控",
    "无驱逐/无重启漂移即爆炸半径未失控",
    "无 Evicted/重启漂移",
)

# Phrases that are legitimate ONLY inside an explicit negation/定案 OR a
# delta-qualified clause (e.g. #10's "勿再按「任何重启漂移即失控」的绝对判据推演",
# or the correct "相对自身基线新增的重启漂移即失控"). A STANDALONE absolute
# occurrence (no negation, no delta qualifier) is naive.
_NEGATION_GUARDED = ("重启漂移即", "漂移即爆炸半径", "漂移即失控")
_LEGIT_CONTEXT_MARKERS = (
    # explicit negation / 定案 of the absolute form
    "勿再", "不是", "定案", "非绝对", "勿按", "不再", "严禁",
    # delta qualifiers that make an "…即失控" clause correct
    "相对自身基线", "注入新增", "新增的", "相对基线", "对照自身基线",
)


def _catalogue_docs() -> list[pathlib.Path]:
    assert _CATALOGUE.is_dir(), f"catalogue dir moved or renamed: {_CATALOGUE}"
    return sorted(_CATALOGUE.rglob("*.md"))


def test_skill_md_single_sources_blast_radius_delta_discipline() -> None:
    """SKILL.md must legislate the general delta-from-own-baseline blast-radius
    discipline once, so every case (present and future) inherits it and the
    planner applies it instead of re-deriving it each run."""
    text = _SKILL_MD.read_text(encoding="utf-8", errors="replace")
    assert "逐 Pod 对照自身基线" in text, (
        "SKILL.md lost the single-sourced blast-radius discipline: the criterion "
        "must be per-pod delta-from-own-baseline (逐 Pod 对照自身基线), not an "
        "absolute same-node drift check — the framework's ContainerRestartDetector "
        "is already delta-based"
    )
    assert "预存" in text and "自重" in text, (
        "SKILL.md blast-radius discipline must instruct locking pre-existing "
        "abnormal pods (预存异常 Pod) whose self-drift (自重) during the window is "
        "baseline-internal, not injection-caused — a production node's baseline is "
        "never clean"
    )


def test_no_catalogue_doc_carries_naive_absolute_blast_radius_criterion() -> None:
    """No catalogue case doc may frame the blast-radius cross-check as a naive
    ABSOLUTE state. Each pinned-absent phrasing ignores pre-existing self-drifting
    pods; reverting any doc to the absolute form turns this red."""
    offenders: list[str] = []
    for doc in _catalogue_docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        for pat in _NAIVE_ABSOLUTE_PATTERNS:
            if pat in text:
                offenders.append(f"{doc.name}: {pat!r}")
    assert not offenders, (
        "these catalogue docs still carry a naive ABSOLUTE blast-radius drift "
        "criterion (must be per-pod delta-from-own-baseline with pre-existing-pod "
        "exclusion):\n  " + "\n  ".join(offenders)
    )


def test_absolute_drift_phrasing_survives_only_inside_negation() -> None:
    """A standalone "重启漂移即…失控" is naive; it may survive only inside an
    explicit negation/定案 sentence (the #10 doc's warning not to reason that way)."""
    offenders: list[str] = []
    for doc in _catalogue_docs():
        for line in doc.read_text(encoding="utf-8", errors="replace").splitlines():
            if any(p in line for p in _NEGATION_GUARDED):
                if not any(m in line for m in _LEGIT_CONTEXT_MARKERS):
                    offenders.append(f"{doc.name}: {line.strip()!r}")
    assert not offenders, (
        "these catalogue lines carry a standalone absolute drift criterion that is "
        "not an explicit negation (blast radius must be judged by per-pod "
        "delta-from-own-baseline):\n  " + "\n  ".join(offenders)
    )
