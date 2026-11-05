"""Sentinel: the ResourceQuota-exceeded Pod_Pending case doc must size the prop
quota's ``hard=pods`` from the NON-TERMINAL pod count, NOT from the raw
``kubectl get pods`` row count, and must gate the scale-up behind a ``used >= hard``
readback.

Root cause this sentinel pins (a case-doc 判据 defect, not a code gap): the doc
used to instruct ``kubectl create quota --hard=pods=<不超过当前Pod总数>`` with the
baseline captured via a bare ``kubectl get pods --no-headers`` row count. But
ResourceQuota's ``pods`` resource counts ONLY non-terminal pods by upstream
Kubernetes semantics — a pod is terminal when ``.status.phase`` is Succeeded or
Failed (shown as Completed/Error in the STATUS column), and such pods are
excluded from ``status.used``. When a namespace carries accumulated terminal
Job/CronJob pods, the raw row count is inflated well above the quota's actual
``used``; sizing ``hard`` from it makes ``hard`` float ABOVE ``used``, leaving
admission headroom, so the scaled-up replica is ADMITTED instead of rejected and
the fault signature (READY<DESIRED, FailedCreate ``exceeded quota``) never
appears. The larger the terminal-pod fraction, the more reliably the method
fails; it only "works" when terminal pods are a negligible fraction (``used ≈
raw count``), which is why the defect stays latent on a clean namespace.

The fix is stable universal fact (upstream ResourceQuota ``pods`` counts
non-terminal pods only; ``--field-selector=status.phase!=Succeeded,status.phase!=Failed``
is the canonical way to enumerate them), NOT cluster-relative — so it belongs in
the doc. The scale-time ``used >= hard`` readback gate makes the exact margin
non-critical: binding is verified against the live quota before the replica is
scaled out.
"""

from __future__ import annotations

import pathlib

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_QUOTA_CASE = (
    _SKILLS_DIR
    / "k8s-chaos-skills"
    / "references"
    / "catalogue"
    / "Pod_Pending"
    / "Pod_Pending_ResourceQuota超限.md"
)


def _text() -> str:
    assert _QUOTA_CASE.exists(), (
        f"ResourceQuota-exceeded case doc moved or renamed: {_QUOTA_CASE}"
    )
    return _QUOTA_CASE.read_text(encoding="utf-8", errors="replace")


def test_quota_hard_sized_from_nonterminal_pod_count() -> None:
    """The ``hard=pods`` sizing basis must be the non-terminal pod count, and the
    doc must state the upstream counting semantics that make the raw row count
    wrong.

    Without this the planner sizes ``hard`` from ``kubectl get pods`` raw rows
    (which include terminal Job/CronJob residue), ``hard`` lands above the quota's
    ``used``, the scaled-up replica is admitted, and the fault never fires.
    """
    text = _text()
    assert "非终止态 Pod 数" in text, (
        "ResourceQuota case doc lost the non-terminal-pod-count sizing basis for "
        "hard=pods — ResourceQuota's `pods` resource counts only non-terminal "
        "(Pending/Running) pods by upstream semantics; sizing hard from the raw "
        "`kubectl get pods` row count (which includes Succeeded/Failed residue) "
        "leaves admission headroom so the new replica is admitted and the fault "
        "never appears"
    )
    assert "只统计非终止态" in text, (
        "ResourceQuota case doc must state the upstream fact that the quota `pods` "
        "resource counts ONLY non-terminal pods — this is the mechanism that makes "
        "the raw row count the wrong sizing basis"
    )
    assert "Succeeded/Failed" in text and "Completed/Error" in text, (
        "ResourceQuota case doc must map the terminal phases (Succeeded/Failed) to "
        "their STATUS-column display names (Completed/Error) so the operator knows "
        "which rows to exclude when counting the sizing basis"
    )


def test_quota_baseline_uses_nonterminal_field_selector() -> None:
    """The baseline-capture command must enumerate non-terminal pods via the
    phase field-selector, not a bare row count."""
    text = _text()
    assert "--field-selector=status.phase!=Succeeded,status.phase!=Failed" in text, (
        "ResourceQuota case doc must count the sizing basis with the canonical "
        "non-terminal field-selector "
        "(`--field-selector=status.phase!=Succeeded,status.phase!=Failed`) — a bare "
        "`kubectl get pods --no-headers` row count includes terminal pods and "
        "over-sizes hard"
    )


def test_quota_scale_gated_by_used_ge_hard_readback() -> None:
    """The scale-up must be gated behind a ``used >= hard`` readback so a
    mis-sized (too-high) hard is caught before the replica is created."""
    text = _text()
    assert "used >= hard" in text, (
        "ResourceQuota case doc lost the `used >= hard` binding readback gate — "
        "without verifying the quota is actually binding before scaling, a hard "
        "sized too high (from the raw row count) silently admits the new replica "
        "and the fault never fires"
    )
    assert "{.status.used.pods}/{.status.hard.pods}" in text, (
        "ResourceQuota case doc must give the concrete readback query "
        "(`-o jsonpath='{.status.used.pods}/{.status.hard.pods}'`) that confirms "
        "the quota is binding before the scale-up"
    )


def test_quota_raw_count_sizing_wording_removed() -> None:
    """The defective raw-row-count sizing wordings must not come back.

    Each pinned-absent here is a standalone instruction to size ``hard`` from the
    current/total pod count without qualifying it as non-terminal; reverting any
    of the doc edits reintroduces one and turns this red.
    """
    text = _text()
    assert "<不超过当前Pod总数>" not in text, (
        "the defective `--hard=pods=<不超过当前Pod总数>` sizing regressed — it uses "
        "the raw pod count (terminal pods included) which over-sizes hard above the "
        "quota's used; must be the non-terminal count minus a small margin"
    )
    assert "<不超过当前Pod数>" not in text, (
        "the defective `--hard=pods=<不超过当前Pod数>` sizing regressed — same raw-count "
        "over-sizing trap; must be the non-terminal count"
    )
    assert "创建上限低于当前 Pod 数的配额" not in text, (
        "the defective injection-step wording '创建上限低于当前 Pod 数的配额' regressed "
        "— 'current pod count' is ambiguous between raw and non-terminal; must say "
        "the quota压住 the non-terminal pod count"
    )
    assert "记录 Pod 总数与 workload 副本数" not in text, (
        "the defective baseline-capture wording '记录 Pod 总数与 workload 副本数' "
        "regressed — the sizing basis must be the NON-TERMINAL pod count, not the "
        "raw total"
    )


def test_quota_nonterminal_additions_carry_no_empirical_narrative() -> None:
    """The added sizing guidance must stay cross-cluster neutral knowledge
    (cbe1ca62): no 实测/轮次/案例号/日期 narrative may leak into the new wording."""
    text = _text()
    for marker in ("实测", "首测", "复测", "重测", "attempt", "#13"):
        for line in text.splitlines():
            if "非终止态" in line or "used >= hard" in line:
                assert marker not in line, (
                    f"ResourceQuota case doc carries empirical-narrative marker "
                    f"{marker!r} inside the non-terminal sizing guidance "
                    f"(cbe1ca62 forbids 实证叙事 in skill docs): {line.strip()!r}"
                )
