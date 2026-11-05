"""Sentinel: the Pod process-suspend case doc's PATH-B (node-side cgroup
freezer) arm must (1) GC its transient thaw unit unconditionally via
``systemd-run --collect``, (2) state that path B carries NO timer-teardown
obligation — banning ``systemctl``/``pkill`` cleanup steps, and (3) treat
``/proc`` State as S-or-D depending on kernel/cgroup version, neither being a
death sentence for the injection.

Root causes this sentinel pins (#11-R-live deep-dive):

1. ``--collect`` gap. Path B arms the reversal as a host-managed
   ``systemd-run --on-active=<recovery-seconds>s --unit=blade-thaw-*`` transient
   unit. Without ``--collect`` systemd only GCs a transient unit that exits 0;
   a failed unit is NOT collected and lingers. The sibling Node-CPU case already
   uses ``--collect`` precisely so the unit is reclaimed unconditionally (fire
   success or failure) and leaves zero residue. Path B's omission meant residue
   was possible by construction.

2. Teardown-obligation gap → wasted planning round. Because the doc never said
   "path B has no timer to tear down", the agent re-derived a cleanup step every
   run and invented ``systemctl stop/reset-failed blade-thaw-*``. ``systemctl``
   is a word-level host-domain banned verb (carriers ``_BANNED_HOST_VERBS``) and
   the plan-level banned-verb scan (#59, ``extract_planning_metadata``) rejects
   the whole plan with ``PLAN/GUARD INCOMPATIBILITY`` and forces a v1→v2 rewrite
   — a full planning round burned (~264s observed live). The guard is correct
   (stopping a unit THIS task armed is word-level indistinguishable from
   stopping kubelet); the fix is the doc, not the guard. With ``--collect`` the
   unit self-GCs and THAWED is idempotent, so there is genuinely nothing to tear
   down.

3. W-11R-1 State over-fossilisation. The doc once asserted a single 5.10
   observation — frozen processes "仍显示 S" — as a universal claim. A cgroup-v1
   refrigerator places frozen tasks in TASK_UNINTERRUPTIBLE, so ``/proc`` State
   may legitimately read D. Both S and D are normal freezer表象; neither proves
   injection failed (nor that D = "deeper/more successful" freeze). Path B's
   effect criterion is carried by ``freezer.state=FROZEN`` + exec hang/refusal
   behavioural contrast, NOT by ``/proc`` State. A single observed letter must
   not be fossilised into a universal assertion.

All three are stable universal facts (systemd ``--collect`` GC semantics;
``systemctl`` being a host banned verb; freezer State being kernel/cgroup
version-dependent), NOT cluster-relative — so per 85a04a51 they belong in the
doc. Each assertion carries a mutation tooth: the negative assertions fail if
the over-fossilised wording is reintroduced.
"""

from __future__ import annotations

import pathlib

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_SUSPEND_CASE = (
    _SKILLS_DIR
    / "k8s-chaos-skills"
    / "references"
    / "catalogue"
    / "Pod_进程异常"
    / "Pod_进程异常_进程被挂起.md"
)


def _text() -> str:
    assert _SUSPEND_CASE.exists(), (
        f"process-suspend case doc moved or renamed: {_SUSPEND_CASE}"
    )
    return _SUSPEND_CASE.read_text(encoding="utf-8", errors="replace")


def test_pathb_arming_uses_collect_for_unconditional_gc() -> None:
    """Path B's thaw timer must arm with ``systemd-run --collect`` so the
    transient unit is GC'd unconditionally (fire success OR failure), matching
    the Node-CPU sibling and guaranteeing zero residue by construction.

    Without ``--collect`` a failed transient unit lingers (systemd only collects
    exit-0 units), reintroducing the residue that tempts a ``systemctl`` cleanup
    step.
    """
    text = _text()
    assert "systemd-run --collect --on-active=<recovery-seconds>s --unit=blade-thaw" in text, (
        "path-B 解冻定时器缺 --collect：瞬态单元 fire 后失败态不被 systemd GC 会留残留，"
        "与 Node_CPU sibling 的零残留构造脱节，并诱发下方禁令里的 systemctl 清理步"
    )


def test_pathb_declares_no_teardown_obligation_and_bans_systemctl() -> None:
    """The doc must定案 that path B carries NO timer-teardown obligation and must
    ban ``systemctl``/``pkill`` cleanup steps.

    This is the 定案免推导 lever: without it the agent re-derives a cleanup step
    every run, invents ``systemctl stop blade-thaw-*``, trips the #59 plan-level
    banned-verb scan (``systemctl`` is a word-level host banned verb), and burns
    a full v1→v2 planning rewrite (~264s live). The guard is correct; the doc
    must steer the agent away from it.
    """
    text = _text()
    assert "路径 B 无任何定时器拆除义务" in text, (
        "path-B 未定案『无定时器拆除义务』：agent 每轮自行发明清理步，撞 #59 plan 级禁词扫描白烧一轮规划"
    )
    assert "systemctl stop/reset-failed blade-thaw" in text, (
        "path-B 未显式点名禁止 systemctl 清理步（negative example）：agent 无从得知该动词会触发 PLAN/GUARD INCOMPATIBILITY 强制重写"
    )


def test_pathb_state_criterion_is_s_or_d_not_fossilised() -> None:
    """Path B's ``/proc`` State criterion must be S-or-D (kernel/cgroup version
    dependent), neither being a death-sentence for the injection.

    W-11R-1: a single 5.10 "仍显示 S" observation was fossilised into a universal
    claim. A cgroup-v1 refrigerator puts frozen tasks in TASK_UNINTERRUPTIBLE,
    so D is equally normal. Path B's effect criterion is carried by
    ``freezer.state=FROZEN`` + exec behavioural contrast, not by State.
    """
    text = _text()
    assert "TASK_UNINTERRUPTIBLE" in text, (
        "path-B State 判据未承认 cgroup-v1 refrigerator→TASK_UNINTERRUPTIBLE（D 态）：单次 S 观测被过度固化"
    )
    assert "也可能转为 D" in text, (
        "path-B State 判据未泛化为 S-or-D：D 态出现时会被误判为注入失败或过度解读"
    )
    assert "均非判死证据" in text, (
        "path-B 未声明 S/D 均非判死证据：State 字母被当作注入成败判据，偏离 freezer.state+exec 行为学对照的真实判据"
    )


def test_over_fossilised_state_wording_not_reintroduced() -> None:
    """Mutation tooth: the over-fossilised single-observation wording must NOT
    come back. If a future edit re-asserts "仍显示 S、内核栈仍显示原睡眠栈" as a
    universal claim, or re-hardcodes the "云内核 5.10 级" version pin, this fails.
    """
    text = _text()
    assert "云内核 5.10 级" not in text, (
        "过度固化的『云内核 5.10 级』版本钉被重新引入：单次集群观测不得写成普世断言（W-11R-1 回归）"
    )


def test_doc_not_vacuous() -> None:
    """Anti-vacuous guard: the sentinel must be scanning a real, non-trivial
    doc, not an empty/renamed stub."""
    text = _text()
    assert len(text) > 4000, (
        f"process-suspend case doc suspiciously small ({len(text)} bytes) — sentinel may be vacuous"
    )
    assert "路径 B" in text and "freezer" in text.lower(), (
        "doc no longer describes path B / cgroup freezer — sentinel anchors drifted"
    )
