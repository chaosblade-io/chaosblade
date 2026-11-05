"""Sentinel: the Pod disk-fill case doc must enumerate ALL THREE consequence
chains a no-independent-volume (rootfs overlay) fill raises — not just node
DiskPressure — and must not carry the misleading "fallocate 仅分配元数据" wording.

Root cause this sentinel pins (#9-R2 time-account, inject-76d369fa): the
planning LLM spent iterations 4–5 (~2 × 60s llm-call) deriving from scratch two
consequence chains the case doc did not cover — its own words, ``run.log``:
"Two consequence-chain risks the spec didn't cover" / "The spec did NOT mention
checking ephemeral-storage limits!":

1. **Pod ephemeral-storage limit eviction** — a rootfs/emptyDir fill counts
   against the Pod's ``ephemeral-storage``; if the container declares a limit and
   the fill exceeds it, kubelet evicts THAT Pod (single-pod) while node
   ``DiskPressure`` stays False, so the node-condition + same-node-pod-list
   cross-check the doc mandated is blind to it. The doc enumerated only the
   node-wide DiskPressure path.

2. **fallocate viability on overlayfs** — ``ovl_fallocate`` forwards fallocate to
   the upper fs only on kernel ≥ 4.16; older kernels return ``EOPNOTSUPP``. The
   LLM had to re-derive this kernel fact AND the read-only way to check it
   (``kubectl get node -o jsonpath={.status.nodeInfo.kernelVersion}``), even
   agonising over whether to spin a debug pod (which would add an 18th pod and
   pollute the blast-radius cross-check). Compounding it, the doc's
   "fallocate（快，仅分配元数据）" wording is factually wrong — fallocate
   allocates REAL blocks (df-used and du both rise by the fill size; verified
   ``du -k`` = 8388612 KB on the live run), which is exactly the physical basis
   of the df-increment main criterion.

Both are stable universal mechanism facts (K8s per-pod ephemeral-storage
enforcement; the kernel ≥ 4.16 ovl_fallocate threshold — the same class as
"busybox has no curl"), NOT cluster-relative and NOT per-tag/per-run偶发, and
neither is programmatically surfaced by preplan_probe — so per the 定案免推导
lever they belong in the doc, letting the next run APPLY a documented check
instead of RE-DERIVING the consequence chain. This sentinel keeps the enumeration
complete (3 axes) and the corrected fallocate semantics pinned.
"""

from __future__ import annotations

import pathlib

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_DISKFILL_CASE = (
    _SKILLS_DIR
    / "k8s-chaos-skills"
    / "references"
    / "catalogue"
    / "Pod_磁盘空间使用率过高"
    / "Pod_磁盘空间使用率过高_应用日志数据积累.md"
)


def _text() -> str:
    assert _DISKFILL_CASE.exists(), (
        f"disk-fill case doc moved or renamed: {_DISKFILL_CASE}"
    )
    return _DISKFILL_CASE.read_text(encoding="utf-8", errors="replace")


def test_diskfill_documents_ephemeral_storage_eviction_path() -> None:
    """The second eviction path (per-Pod ephemeral-storage limit) must be legislated.

    A no-independent-volume fill counts against the Pod's ephemeral-storage; the
    doc must tell the planner to check the container limit and warn that exceeding
    it evicts the target Pod while node DiskPressure stays False (invisible to the
    node-condition cross-check). Without this the LLM re-derives the consequence
    chain every run (#9-R2 iter4–5).
    """
    text = _text()
    assert "ephemeral-storage" in text, (
        "disk-fill case doc lost the ephemeral-storage eviction path — a "
        "rootfs/emptyDir fill counts against the Pod's ephemeral-storage and can "
        "get the target Pod evicted (node DiskPressure stays False); omitting it "
        "forces the planning LLM to re-derive the consequence chain (#9-R2)"
    )
    # The probe that reads the container limit must be present.
    assert ".resources.limits.ephemeral-storage" in text, (
        "disk-fill case doc must give the read-only probe for the container "
        "ephemeral-storage limit (.spec.containers[*].resources.limits."
        "ephemeral-storage) so the planner applies a documented check"
    )
    # The single-Pod-vs-node-wide distinction must be explicit.
    assert "DiskPressure" in text and ("单 Pod" in text or "该 Pod" in text), (
        "disk-fill case doc must state that ephemeral-storage-limit eviction "
        "hits the SINGLE target Pod while node DiskPressure stays False — the "
        "distinction that makes the node-condition cross-check insufficient"
    )


def test_diskfill_documents_fallocate_overlayfs_kernel_threshold() -> None:
    """fallocate-on-overlayfs viability (kernel ≥ 4.16) must be 定案免推导.

    ovl_fallocate forwards fallocate only on kernel ≥ 4.16 (else EOPNOTSUPP); the
    doc must state the threshold AND the read-only node-kernelVersion probe, so
    the planner doesn't re-derive the kernel fact or agonise over a debug-pod
    backing-fs check (#9-R2 iter4–5).
    """
    text = _text()
    assert "4.16" in text and "ovl_fallocate" in text, (
        "disk-fill case doc lost the fallocate-on-overlayfs viability fact "
        "(ovl_fallocate forwards only on kernel ≥ 4.16, else EOPNOTSUPP) — the "
        "planning LLM had to re-derive this kernel threshold from scratch (#9-R2)"
    )
    assert ".status.nodeInfo.kernelVersion" in text, (
        "disk-fill case doc must give the READ-ONLY node kernelVersion probe as "
        "the way to settle fallocate viability, so the planner doesn't spin a "
        "debug pod (which adds a transient pod and pollutes the blast-radius "
        "same-node inventory cross-check)"
    )
    # dd must be documented as the in-path-A fallback (NOT a degrade to 手段1).
    assert "dd" in text and "EOPNOTSUPP" in text, (
        "disk-fill case doc must name dd as the in-path-A fallback when fallocate "
        "returns EOPNOTSUPP on kernel < 4.16 (same mechanism, not a degrade to "
        "ChaosBlade 手段1)"
    )


def test_diskfill_fallocate_semantics_corrected() -> None:
    """The misleading "fallocate（快，仅分配元数据）" wording must stay corrected.

    fallocate allocates REAL blocks (df-used and du both rise by the fill size) —
    that is the physical basis of the df-increment main criterion. "仅分配元数据"
    wrongly implies sparse/no real allocation and contradicts the effect criterion.
    """
    text = _text()
    # The corrected positive semantics must be present.
    assert "真实磁盘块" in text, (
        "disk-fill case doc must state fallocate allocates REAL disk blocks "
        "(df-used/du rise by the fill size) — the physical basis of the df-increment "
        "main criterion"
    )
    # The old misleading STANDALONE claim must not come back. (The phrase
    # "仅分配元数据" may still appear only inside an explicit negation.)
    assert "fallocate（快，仅分配元数据）" not in text, (
        "the misleading standalone wording 'fallocate（快，仅分配元数据）' "
        "regressed — fallocate allocates real blocks, not merely metadata"
    )
