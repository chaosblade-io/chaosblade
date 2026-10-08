"""Tests for chaos_agent.agent.experiment_display — active experiment rendering."""

from chaos_agent.agent.experiment_display import format_experiment_line


def _line(experiment: dict) -> str:
    # format_experiment_line calls format_relative_time() without a now
    # override, so these cases assert the structural pieces (fault type,
    # target, description) that don't depend on wall-clock; the time label
    # itself is covered in test_time.py.
    return format_experiment_line(1, experiment)


def test_uses_fault_type_not_skill_package():
    """The real fault_type is shown, not the generic skill package name."""
    line = _line({
        "task_id": "task-abc",
        "skill": "k8s-chaos-skills",
        "fault_type": "pod-image-error",
        "target": {"namespace": "reg-center", "names": ["registry-sts"]},
    })
    assert "pod-image-error" in line
    assert "k8s-chaos-skills" not in line


def test_falls_back_to_skill_when_no_fault_type():
    line = _line({"task_id": "t1", "skill": "custom-skill", "target": {}})
    assert "custom-skill" in line


def test_target_namespace_and_name():
    line = _line({
        "task_id": "t1",
        "fault_type": "pod-cpu-fullload",
        "target": {"namespace": "reg-center", "names": ["registry-sts"]},
    })
    assert "reg-center/registry-sts" in line


def test_target_labels_when_no_names():
    line = _line({
        "task_id": "t1",
        "fault_type": "pod-cpu-fullload",
        "target": {"namespace": "taokeeper", "labels": {"app": "taokeeper"}},
    })
    assert "taokeeper (app=taokeeper)" in line


def test_target_name_fallback_column():
    line = _line({
        "task_id": "t1",
        "fault_type": "pod-cpu-fullload",
        "target": {"namespace": "default"},
        "target_name": "pod-x",
    })
    assert "default/pod-x" in line


def test_includes_relative_time_prefix():
    """A parseable gmt_create yields a bracketed time prefix."""
    line = format_experiment_line(1, {
        "task_id": "t1",
        "fault_type": "pod-cpu-fullload",
        "target": {"namespace": "default"},
        "gmt_create": "2026-06-23T15:02:00+08:00",
    })
    # We don't assert the exact label (depends on wall-clock) but the prefix
    # bracket must be present when gmt_create is parseable.
    assert line.strip().startswith("1. [")


def test_omits_time_prefix_when_no_gmt_create():
    line = format_experiment_line(1, {
        "task_id": "t1",
        "fault_type": "pod-cpu-fullload",
        "target": {"namespace": "default"},
    })
    assert "[" not in line
    assert "task_id=t1" in line


def test_includes_plan_summary_first_line():
    line = _line({
        "task_id": "t1",
        "fault_type": "pod-image-error",
        "target": {"namespace": "reg-center", "names": ["registry-sts"]},
        "plan_summary": "将 StatefulSet registry-sts 镜像改为无效值\n第二行应被忽略",
    })
    assert "description: 将 StatefulSet registry-sts 镜像改为无效值" in line
    assert "第二行应被忽略" not in line


def test_missing_fields_degrade_gracefully():
    """An almost-empty dict must not raise and still renders task_id."""
    line = format_experiment_line(3, {})
    assert "3." in line
    assert "task_id=?" in line


# -- carrier fact line (Plan A presentation: facts, never a verdict) --------

def test_carrier_line_renders_cleaned_recovery_carrier():
    line = _line({
        "task_id": "t1",
        "fault_type": "pod-process-kill",
        "target": {"namespace": "default", "names": ["api-0"]},
        "execution_artifacts": [
            {"type": "recovery_carrier", "status": "cleaned"},
        ],
    })
    assert "carrier: recovery_carrier=cleaned" in line
    # states observation only — no verdict words
    assert "cleared" not in line.lower() or "recovery_void" in line


def test_carrier_line_flags_recovery_void():
    line = _line({
        "task_id": "t1",
        "fault_type": "pod-network-loss",
        "target": {"namespace": "default", "names": ["api-0"]},
        "execution_artifacts": [
            {"type": "recovery_carrier", "status": "cleaned", "recovery_void": True},
        ],
    })
    assert "recovery_void" in line


def test_carrier_line_labels_probe_only_channel():
    """A debug_pod is a probe, not a reversal carrier — the line must say so
    rather than imply the fault was torn down."""
    line = _line({
        "task_id": "t1",
        "fault_type": "pod-mem-load",
        "target": {"namespace": "default", "names": ["api-0"]},
        "execution_artifacts": [
            {"type": "debug_pod", "status": "cleaned"},
        ],
    })
    assert "probe channel" in line
    assert "no teardown" in line


def test_carrier_line_none_registered():
    line = _line({
        "task_id": "t1",
        "fault_type": "pod-network-drop",
        "target": {"namespace": "kube-system", "names": ["coredns-x"]},
        "execution_artifacts": [],
    })
    assert "carrier: none registered" in line


def test_carrier_line_host_timer_deadline_state():
    import time as _time
    passed = _line({
        "task_id": "t1",
        "fault_type": "pod-network-delay",
        "target": {"namespace": "default", "names": ["api-0"]},
        "execution_artifacts": [{
            "type": "recovery_carrier", "status": "cleaned",
            "recovery_form": "host_timer",
            "recovery_deadline_epoch": _time.time() - 10,
        }],
    })
    assert "host_timer deadline passed" in passed
    pending = _line({
        "task_id": "t1",
        "fault_type": "pod-network-delay",
        "target": {"namespace": "default", "names": ["api-0"]},
        "execution_artifacts": [{
            "type": "recovery_carrier", "status": "cleaned",
            "recovery_form": "host_timer",
            "recovery_deadline_epoch": _time.time() + 3600,
        }],
    })
    assert "host_timer deadline pending" in pending
