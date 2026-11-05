"""Sentinel: the Pod disk-IO-load case doc's blast-radius criterion must be
DELTA-FROM-EACH-POD'S-OWN-BASELINE (not a naive absolute "any restart drift =
loss of control"), must lock pre-existing abnormal pods, and must use
truncation-safe compact jsonpath queries for the same-node census.

Root cause this sentinel pins (#10-R deep-dive, inject-4f46bdff): the doc framed
the blast-radius baseline as an ABSOLUTE state check — "任何 Evicted/重启漂移即
爆炸半径失控" / "同节点 Pod 与基线清单一致（无异常重启）" / "Pod 重启/RESTARTS
漂移…出现任一项 = 爆炸半径失控". But a production node's baseline is NOT clean:
it always carries pre-existing abnormal pods. In #10-R the same-node census held
``payment-gateway`` in Init:CrashLoopBackOff at RESTARTS 53075, restarting every
~90s — so during a 300s fault window it drifts by ~3 REGARDLESS of the injection.
Under the naive absolute criterion that self-drift reads as blast-radius loss of
control. The planner had to DERIVE the correct approach from scratch (run log:
"I must record its baseline restart count and exclude it from the
blast-radius-drift criterion … compare against its own baseline, expecting
continued drift from its own CrashLoopBackOff, not from IO").

This is a doc/code inconsistency, not a code gap: the framework's deterministic
side-effect layer ALREADY does delta-from-baseline (``ContainerRestartDetector``
computes ``delta = current_rc - baseline_rc``, filters restarts whose
``finished_at`` precedes injection, and peels exit-0 natural completions). The
doc's absolute wording contradicts the framework and forces every run to
re-derive the delta judgment (定案免推导 lever, d4ac1868 type-a: a validated
recurring decision with no doc 定案).

Two truncation facets ride the same root: the verbose ``get pods -A -o wide
--no-headers`` census truncated at 5137 bytes on the 29-pod node, and the
one-line ``{range .status.conditions[*]}`` conditions query truncated past the
云厂商 NPD custom conditions — both forced a re-probe with a compact jsonpath
(the aa008b38 established practice). The compact ``containerStatuses[0].
restartCount`` jsonpath ALSO sidesteps the ``-o wide`` RESTARTS column's
initContainer aggregation trap (payment-gateway showed 53075 via -o wide while
its main container's restartCount was 0).

All of this is stable universal fact (production nodes have pre-existing abnormal
pods; the framework detector is delta-based; -o wide truncates and aggregates
init restarts), NOT cluster-relative — so per 85a04a51 it belongs in the doc.
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


def test_diskio_blast_radius_mandates_delta_from_own_baseline() -> None:
    """The blast-radius criterion must be per-pod delta-from-own-baseline and must
    lock pre-existing abnormal pods, not a naive absolute "any drift = loss of
    control".

    Without this the planner re-derives the delta judgment every run and a less
    careful model misattributes a pre-existing CrashLoopBackOff pod's self-drift
    to the injection (#10-R: payment-gateway RESTARTS 53075, restarting ~90s).
    """
    text = _text()
    assert "逐 Pod 对照自身基线" in text, (
        "disk-IO case doc lost the per-pod delta-from-own-baseline blast-radius "
        "criterion — the framework's ContainerRestartDetector already computes "
        "delta = current_rc - baseline_rc; the doc must match, not assert an "
        "absolute 'any restart drift = loss of control'"
    )
    assert "预存" in text and "自重" in text, (
        "disk-IO case doc must instruct locking pre-existing abnormal pods "
        "(预存异常 Pod) whose self-drift (自重) during the window is baseline-internal, "
        "not injection-caused — a production node's baseline is never clean"
    )


def test_diskio_blast_radius_uses_truncation_safe_jsonpath() -> None:
    """The same-node census must use a compact per-pod jsonpath (truncation-safe,
    reads container-level restartCount), not the verbose ``-o wide`` form that
    truncates on production nodes and aggregates initContainer restarts."""
    text = _text()
    assert "containerStatuses[0].restartCount" in text, (
        "disk-IO case doc must census same-node pods via a compact jsonpath reading "
        "container-level restartCount — this is truncation-safe AND sidesteps the "
        "`-o wide` RESTARTS column's initContainer aggregation trap (aa008b38)"
    )
    assert "initContainer" in text, (
        "disk-IO case doc must note the `-o wide` RESTARTS column aggregates "
        "initContainer restarts (payment-gateway showed 53075 via -o wide while its "
        "main container restartCount was 0) so drift is not misattributed"
    )
    assert "-o wide --field-selector spec.nodeName" not in text, (
        "the verbose same-node census `get pods -A -o wide --field-selector "
        "spec.nodeName=… --no-headers` regressed — it truncated at 5137 bytes on a "
        "29-pod node (#10-R), forcing a re-probe; use the compact jsonpath instead"
    )
    assert "{range .status.conditions[*]}{.type}={.status} {end}" not in text, (
        "the one-line全量 node-conditions jsonpath regressed — it truncates past the "
        "云厂商 NPD custom conditions (#10-R re-probe); query the cared-about "
        "conditions with a filtered jsonpath instead"
    )


def test_diskio_naive_absolute_drift_criterion_removed() -> None:
    """The naive ABSOLUTE blast-radius drift assertions must not come back.

    Each pinned-absent here is a standalone "any drift = loss of control" wording
    that ignores pre-existing self-drifting pods; reverting any turns this red.
    """
    text = _text()
    assert "任何 Evicted/重启漂移即" not in text, (
        "the naive absolute '任何 Evicted/重启漂移即爆炸半径失控' regressed — a "
        "pre-existing CrashLoopBackOff pod drifts on its own; the criterion must be "
        "delta-from-own-baseline (the phrase may survive only inside the explicit "
        "negation '勿再按「任何重启漂移即失控」的绝对判据推演')"
    )
    assert "同节点 Pod 与基线清单一致（无" not in text, (
        "the naive '同节点 Pod 与基线清单一致（无异常重启）' regressed — must be "
        "per-pod delta-from-own-baseline with pre-existing-pod exclusion"
    )
    assert "节点 DiskPressure=True、Pod 重启/RESTARTS 漂移" not in text, (
        "the naive 预期阴性 'Pod 重启/RESTARTS 漂移…出现任一项 = 失控' regressed — "
        "it must be qualified as '相对自身基线新增的' drift so a pre-existing "
        "self-drifting pod does not false-trigger loss of control"
    )
    # The negation-embedded mention is legitimate; a STANDALONE absolute
    # "重启漂移即…失控" outside a negation is not.
    for line in text.splitlines():
        if "重启漂移即" in line or "漂移即爆炸半径失控" in line:
            assert "勿再" in line or "不是" in line or "定案" in line, (
                f"disk-IO case doc carries a standalone absolute drift criterion "
                f"that is not an explicit negation: {line.strip()!r} — blast radius "
                f"must be judged by per-pod delta-from-own-baseline"
            )
