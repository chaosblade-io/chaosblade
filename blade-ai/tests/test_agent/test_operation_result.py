from pathlib import Path

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.agent.operation_result import (
    build_inject_data_from_state,
    build_inject_status_data_from_state,
    build_recover_cli_data_from_state,
    build_recover_cli_failure_data_from_state,
    build_recover_data_from_state,
    build_unknown_inject_data,
    recover_result_label_from_values,
    recover_task_state_from_values,
    target_list_from_state,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _inject_state() -> dict:
    spec = FaultSpec(
        namespace="arms-prom",
        scope="pod",
        names=("pod-a",),
        blade_target="cpu",
        blade_action="fullload",
        params={"cpu-percent": "80"},
    )
    return {
        "confirmed_intent": "inject",
        "fault_spec": spec.to_dict(),
        "skill_name": "stale-active-skill",
        "blade_uid": "uid-1",
        "result": {"success": True},
        "verification": {
            "level": "strong",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
            "side_effects": {"container_restarts": []},
        },
    }


def test_build_inject_data_uses_fault_spec_projection():
    data = build_inject_data_from_state(_inject_state(), "task-inject", elapsed_ms=123)

    assert data["task_id"] == "task-inject"
    assert data["task_state"] == "injected"
    assert data["fault_type"] == "pod-cpu-fullload"
    assert data["blade_uid"] == "uid-1"
    assert data["duration_ms"] == 123
    assert data["target"]["namespace"] == "arms-prom"
    assert data["target"]["names"] == ["pod-a"]
    assert data["params"] == {"cpu-percent": "80"}
    assert data["verification"]["level"] == "strong"
    assert "side_effects" not in data["verification"]
    assert data["side_effects"] == {"container_restarts": []}


def test_build_recover_data_uses_inject_state_for_fault_and_target():
    recover_state = {
        "operation": "recover",
        "result": {"recovered": True, "recovery_level": "partial"},
        "recover_verification": {
            "level": "recovered",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
    }

    data = build_recover_data_from_state(
        recover_state,
        "task-recover",
        _inject_state(),
        elapsed_ms=456,
    )

    assert data == {
        "task_id": "task-recover",
        "operation": "recover",
        "task_state": "partial_recovered",
        "fault_type": "pod-cpu-fullload",
        "blade_uid": "uid-1",
        "duration_ms": 456,
        "target": {
            "namespace": "arms-prom",
            "names": ["pod-a"],
            "labels": {},
            "resource_type": "pod",
        },
        "params": {"cpu-percent": "80"},
        "verification": {
            "level": "recovered",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
    }


def test_recover_cli_data_preserves_legacy_shape():
    recover_state = {
        "result": {"recovered": True, "recovery_level": "recovered"},
        "recover_verification": {
            "level": "recovered",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
    }

    data = build_recover_cli_data_from_state(
        recover_state,
        "task-inject",
        _inject_state(),
    )

    assert data == {
        "task_id": "task-inject",
        "result": "recovered",
        "blade_uid": "uid-1",
        "targets": [{"name": "pod-a", "namespace": "arms-prom"}],
        "verification": {
            "level": "recovered",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
            "baseline_confidence": "none",
            "baseline_used": None,
        },
    }


def test_inject_status_data_projects_pending_targets_from_fault_spec():
    data = build_inject_status_data_from_state(
        _inject_state(),
        "task-inject",
        result="pending",
        include_blade_uid=False,
    )

    assert data == {
        "task_id": "task-inject",
        "result": "pending",
        "fault_type": "pod-cpu-fullload",
        "targets": [{"name": "pod-a", "namespace": "arms-prom"}],
    }


def test_inject_status_data_projects_failed_error_from_fault_spec():
    data = build_inject_status_data_from_state(
        {},
        "task-inject",
        result="failed",
        fault_spec=FaultSpec(
            namespace="arms-prom",
            scope="pod",
            names=("pod-a",),
            blade_target="network",
            blade_action="loss",
        ),
        error="internal_error: boom",
    )

    assert data == {
        "task_id": "task-inject",
        "result": "failed",
        "fault_type": "pod-network-loss",
        "targets": [{"name": "pod-a", "namespace": "arms-prom"}],
        "blade_uid": "",
        "error": "internal_error: boom",
    }


def test_unknown_inject_data_uses_complete_result_card_shape():
    assert build_unknown_inject_data("task-inject", blade_uid="uid-1") == {
        "task_id": "task-inject",
        "task_state": "unknown",
        "fault_type": "",
        "blade_uid": "uid-1",
        "duration_ms": 0,
        "target": {},
        "params": {},
        "verification": None,
        "side_effects": None,
        "postmortem": None,
        "error": "",
    }


def test_recover_cli_failure_data_projects_inject_target():
    data = build_recover_cli_failure_data_from_state(
        "task-inject",
        _inject_state(),
        error="internal_error: boom",
    )

    assert data == {
        "task_id": "task-inject",
        "result": "failed",
        "blade_uid": "uid-1",
        "targets": [{"name": "pod-a", "namespace": "arms-prom"}],
        "verification": None,
        "error": "internal_error: boom",
    }


def test_recover_state_helpers_map_failed_and_partial_states():
    assert recover_task_state_from_values({"result": {"recovered": False}}) == "failed"
    assert recover_task_state_from_values(
        {"result": {"recovered": True, "recovery_level": "partial"}}
    ) == "partial_recovered"
    assert recover_result_label_from_values({"result": {"recovered": False}}) == "failed"
    assert recover_result_label_from_values(
        {"result": {"recovered": True, "recovery_level": "partial"}}
    ) == "partial"


def test_target_list_from_state_falls_back_to_fault_spec_projection():
    assert target_list_from_state(_inject_state()) == [
        {"name": "pod-a", "namespace": "arms-prom"}
    ]


def test_operation_result_builders_are_not_imported_from_server_routes():
    """Production callers should consume operation_result, not server wrappers."""

    forbidden_imports = [
        "from chaos_agent.server.routes.turn_result import build_inject_data_from_state",
        "from chaos_agent.server.routes.turn_result import build_recover_data_from_state",
        "from chaos_agent.server.routes.turn_result import build_recover_cli_data_from_state",
    ]
    violations = []
    for path in (PROJECT_ROOT / "src/chaos_agent").rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel == "src/chaos_agent/server/routes/turn_result.py":
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in forbidden_imports:
            if forbidden in text:
                violations.append(f"{rel}: {forbidden}")

    assert violations == []


def test_agent_cli_memory_layers_do_not_depend_on_turn_result_route():
    """Result data ownership must stay below the server route layer."""

    checked_roots = [
        PROJECT_ROOT / "src/chaos_agent/agent",
        PROJECT_ROOT / "src/chaos_agent/cli",
        PROJECT_ROOT / "src/chaos_agent/memory",
    ]
    violations = []
    for root in checked_roots:
        for path in root.rglob("*.py"):
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            if "chaos_agent.server.routes.turn_result" in text:
                violations.append(rel)

    assert violations == []
