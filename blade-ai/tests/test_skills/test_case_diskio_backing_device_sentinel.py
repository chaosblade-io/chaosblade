"""Sentinel: the Pod disk-IO-load case doc must NOT assert the container overlay
is backed by the NODE ROOT DISK — the backing block device is a PER-NODE variable
determined by where containerd's data-root (snapshotter dir) is mounted, and must
be localized by post-injection diskstats differential, never assumed.

Root cause this sentinel pins (#10-R retest, inject-4f46bdff): the doc framed the
IO landing point as "容器根文件系统（节点根盘 overlay）" / "burn 的效果本来就是
节点根盘 IO 饱和" / "必现现象：节点根盘 IO 吞吐异常升高" — asserting the ROOT
DISK. But the backing device of the container overlay is wherever containerd's
data-root lives on THAT node: production nodes commonly mount a DEDICATED DATA
DISK for containerd storage to isolate it from the OS root disk. Measured proof
across two nodes running the SAME case:

* #10 first test (11-pod node): backing device = ``vda3`` (root disk); ``vdb``
  was the quiet cross-check (+2,248 sectors).
* #10-R retest (node i-bp11fabmqqffbml8ysjs, 29 pods): backing device =
  ``vdb1`` (DATA disk; overlay upperdir ``/home/t4/containerd/...``) while
  ``vda3`` stayed flat (+792 sectors) — the EXACT OPPOSITE.

The verifier had to issue a "PLAN ASSUMPTION CORRECTED BY MEASUREMENT" warning
because the planner, following the doc's root-disk framing, declared the write
pressure would land on vda3. This is the same class as the B15 code bug (which
hardcoded a device assumption in the extractor and was fixed to extract ALL
devices): the DOC must likewise stop assuming a device and defer to measurement.

This is a stable universal mechanism fact (containerd data-root placement
determines the overlay backing device; it varies per node), NOT a cluster-relative
fact — so per the 定案免推导 lever it belongs in the doc. The sentinel keeps the
per-node-variable framing pinned and the misleading standalone root-disk
assertions out (the phrase 节点根盘 may survive ONLY inside an explicit negation).
"""

from __future__ import annotations

import pathlib

_SKILLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "skills"
_DISKIO_CASE = (
    _SKILLS_DIR
    / "k8s-chaos-skills"
    / "references"
    / "catalogue"
    / "Pod_磁盘IO过高"
    / "Pod_磁盘IO过高_磁盘IO读写负载.md"
)


def _text() -> str:
    assert _DISKIO_CASE.exists(), (
        f"disk-IO-load case doc moved or renamed: {_DISKIO_CASE}"
    )
    return _DISKIO_CASE.read_text(encoding="utf-8", errors="replace")


def test_diskio_documents_backing_device_is_per_node_variable() -> None:
    """The doc must state the overlay backing device is containerd-data-root-determined
    and varies per node — not a fixed root disk.

    Without this the planner assumes the root disk (vda3) and the verifier must
    correct it by measurement (#10-R: backing was vdb1, a data disk).
    """
    text = _text()
    assert "containerd data-root" in text, (
        "disk-IO case doc lost the containerd-data-root determination of the "
        "overlay backing device — the backing block device is wherever containerd's "
        "data-root is mounted on that node, which production commonly places on a "
        "dedicated data disk; omitting this makes the planner assume the root disk "
        "(#10-R measured vdb1 data disk, not vda3 root)"
    )
    assert "随节点而变" in text, (
        "disk-IO case doc must state the backing device is a PER-NODE variable "
        "(随节点而变) — the same case measured vda3 (root) on one node and vdb1 "
        "(data) on another, so no single device can be assumed"
    )


def test_diskio_mandates_differential_localization_not_assumption() -> None:
    """The doc must mandate localizing the backing device by diskstats differential,
    never presetting a device name / the root disk."""
    text = _text()
    assert "差分定位" in text or "不预设" in text, (
        "disk-IO case doc must instruct localizing the backing device by "
        "post-injection diskstats differential (差分定位) and never presetting a "
        "device name — the differential-localization method is the only node-agnostic "
        "way to identify the effect device"
    )


def test_diskio_misleading_root_disk_assertions_removed() -> None:
    """The misleading STANDALONE root-disk assertions must not come back.

    节点根盘 may appear ONLY inside an explicit negation ("不是固定的「节点根盘」").
    The three original standalone framings that asserted the root disk are pinned
    absent here; reverting any of them turns this test red.
    """
    text = _text()
    # Original misleading standalone framings (pre-fix) must be absent.
    assert "节点根盘\n   overlay" not in text, (
        "the misleading framing '容器根文件系统（节点根盘 overlay）' regressed — "
        "the overlay backing device is not necessarily the root disk (#10-R: vdb1)"
    )
    assert "本来就是节点根盘 IO 饱和" not in text, (
        "the misleading framing 'burn 的效果本来就是节点根盘 IO 饱和' regressed — "
        "burn saturates the container overlay backing device, which varies per node"
    )
    assert "必现现象**：节点根盘 IO 吞吐异常升高" not in text, (
        "the misleading '必现现象：节点根盘 IO 吞吐异常升高' regressed — the effect "
        "device must be stated as the overlay backing device measured by differential"
    )
    # Every surviving 节点根盘 occurrence must be inside an explicit negation.
    for line in text.splitlines():
        if "节点根盘" in line:
            assert "不是" in line or "不预设" in line or "切勿" in line, (
                f"disk-IO case doc carries a standalone 节点根盘 assertion that is "
                f"not an explicit negation: {line.strip()!r} — the backing device "
                f"is per-node variable and must not be asserted as the root disk"
            )
