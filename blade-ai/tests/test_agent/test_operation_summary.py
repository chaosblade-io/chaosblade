from pathlib import Path

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.agent.operation_summary import (
    build_batch_summary_text,
    build_recover_summary_text,
    build_task_summary_text,
    format_summary_target,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_format_summary_target_prefers_names_then_labels():
    assert (
        format_summary_target({"namespace": "arms-prom", "names": ["pod-a", "pod-b"]})
        == "arms-prom/pod-a, pod-b"
    )
    assert (
        format_summary_target({"namespace": "arms-prom", "labels": {"app": "api"}})
        == "arms-prom/app=api"
    )


def test_task_summary_uses_fault_spec_not_active_skill_name():
    spec = FaultSpec(
        namespace="arms-prom",
        scope="pod",
        names=("pod-a",),
        blade_target="network",
        blade_action="delay",
    )
    text = build_task_summary_text(
        {
            "fault_spec": spec.to_dict(),
            "skill_name": "k8s-pod-network-delay-skill",
            "blade_uid": "uid-1",
            "result": {"success": True},
            "verification": {
                "level": "strong",
                "layer1": {"status": "passed"},
                "layer2": {"status": "passed"},
            },
        },
        "task-inject",
    )

    assert text.startswith("[Task Summary] task_id=task-inject")
    assert "类型: pod-network-delay | 目标: arms-prom/pod-a" in text
    assert "结果: injected | blade_uid: uid-1" in text
    assert "验证: strong (L1=passed, L2=passed)" in text
    assert "若要复用这些目标，必须重新 kubectl 验证当前存在性" in text


def test_batch_summary_contains_targets_failures_and_freshness_note():
    text = build_batch_summary_text(
        [
            {
                "task_id": "task-a",
                "task_state": "injected",
                "fault_type": "pod-pod-delete",
                "target": {"namespace": "arms-prom", "names": ["pod-a"]},
            },
            {
                "task_id": "task-b",
                "task_state": "failed",
                "fault_type": "pod-pod-delete",
                "target": {"namespace": "arms-prom", "labels": {"app": "api"}},
                "failure_reason": "pod not found",
            },
        ],
        "/tmp/batch.md",
    )

    assert text.startswith("[Batch Summary] 2 faults")
    assert "操作: batch_inject" in text
    assert "target=arms-prom/pod-a" in text
    assert "target=arms-prom/app=api" in text
    assert "失败原因: pod not found" in text
    assert "批量分析报告: /tmp/batch.md" in text
    assert "本概要及更早历史中的资源名仅作历史上下文" in text


def test_recover_summary_falls_back_to_inject_state_fault_type():
    spec = FaultSpec(
        namespace="arms-prom",
        scope="pod",
        names=("pod-a",),
        blade_target="cpu",
        blade_action="fullload",
    )
    text = build_recover_summary_text(
        {
            "data": {
                "task_id": "task-recover",
                "task_state": "recovered",
                "blade_uid": "uid-1",
                "target": {"namespace": "arms-prom", "names": ["pod-a"]},
                "verification": {
                    "level": "recovered",
                    "layer1": {"status": "passed"},
                    "layer2": {"status": "passed"},
                },
            },
        },
        "task-inject",
        {"fault_spec": spec.to_dict(), "blade_uid": "uid-1"},
    )

    assert text.startswith("[Recover Summary] task_id=task-recover")
    assert "parent_task_id: task-inject" in text
    assert "类型: pod-cpu-fullload | 目标: arms-prom/pod-a" in text
    assert "结果: recovered | blade_uid: uid-1" in text
    assert "恢复验证: recovered (L1=passed, L2=passed)" in text
    assert "若要复用这些目标，必须重新 kubectl 验证当前存在性" in text


def test_recover_summary_falls_back_to_inject_state_target_only():
    spec = FaultSpec(
        namespace="arms-prom",
        scope="pod",
        names=("pod-a",),
        blade_target="cpu",
        blade_action="fullload",
    )
    text = build_recover_summary_text(
        {
            "data": {
                "task_id": "task-recover",
                "task_state": "recovered",
                "fault_type": "pod-cpu-fullload",
                "blade_uid": "uid-1",
                "verification": {
                    "level": "recovered",
                    "layer1": {"status": "passed"},
                    "layer2": {"status": "passed"},
                },
            },
        },
        "task-inject",
        {
            "fault_spec": spec.to_dict(),
            "verification": {
                "level": "inject-verified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "passed"},
            },
        },
    )

    assert "类型: pod-cpu-fullload | 目标: arms-prom/pod-a" in text
    assert "恢复验证: recovered (L1=passed, L2=passed)" in text
    assert "inject-verified" not in text


def test_recover_summary_keeps_empty_verification_compatibility_line():
    text = build_recover_summary_text(
        {
            "data": {
                "task_id": "task-recover",
                "task_state": "recovered",
                "fault_type": "pod-cpu-fullload",
                "verification": {},
            },
        },
        "task-inject",
        {},
    )

    assert "恢复验证: ? (L1=?, L2=?)" in text


def test_operation_summary_markers_stay_in_builder_and_trim_preserve_rule():
    """Production code should not hand-roll operation summary marker strings."""

    allowed = {
        "src/chaos_agent/agent/operation_summary.py",
        "src/chaos_agent/agent/nodes/intent_confirm.py",
    }
    markers = ("[Task Summary]", "[Batch Summary]", "[Recover Summary]")

    violations = []
    for path in (PROJECT_ROOT / "src/chaos_agent").rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(marker in line for marker in markers) and rel not in allowed:
                violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert violations == []
