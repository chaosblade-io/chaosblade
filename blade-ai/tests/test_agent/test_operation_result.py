from pathlib import Path

from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.result.operation_result import (
    build_inject_data_from_state,
    build_inject_status_data_from_state,
    build_recover_cli_data_from_state,
    build_recover_cli_failure_data_from_state,
    build_recover_data_from_state,
    build_recovery_handle,
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
        fault_target="cpu",
        fault_action="fullload",
        params={"cpu-percent": "80"},
    )
    return {
        "confirmed_intent": "inject",
        "fault_spec": spec.to_dict(),
        "skill_name": "stale-active-skill",
        "experiment_uid": "uid-1",
        "result": {"success": True},
        "verification": {
            "level": "verified",
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
    # Output key face is the modern spelling only (no legacy mirror;
    # phase-14 G4 retired the legacy-input hydration these tests once
    # exercised — the fixture carries the modern key directly).
    assert data["experiment_uid"] == "uid-1"
    assert "blade_uid" not in data
    assert data["duration_ms"] == 123
    assert data["fault_spec"] == _inject_state()["fault_spec"]
    assert data["target"]["namespace"] == "arms-prom"
    assert data["target"]["names"] == ["pod-a"]
    assert data["params"] == {"cpu-percent": "80"}
    assert data["verification"]["level"] == "verified"
    assert "side_effects" not in data["verification"]
    assert data["side_effects"] == {"container_restarts": []}


def test_build_recover_data_uses_inject_state_for_fault_and_target():
    # Write-path invariant (round-15 D4): result.recovery_level mirrors
    # recover_verification.level — the fixture used to carry the divergent
    # pair (result="partial" vs verification="recovered"), which D4's
    # verification-authoritative ruling resolves to "recovered". Made
    # self-consistent here so the test keeps exercising the partial path.
    recover_state = {
        "operation": "recover",
        "result": {"recovered": True, "recovery_level": "partial"},
        "recover_verification": {
            "level": "partial",
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
        "experiment_uid": "uid-1",
        "recovery_handle": {
            "kind": "experiment_uid",
            "value": "uid-1",
            "experiment_uid": "uid-1",
        },
        "duration_ms": 456,
        "fault_spec": _inject_state()["fault_spec"],
        "target": {
            "namespace": "arms-prom",
            "names": ["pod-a"],
            "labels": {},
            "resource_type": "pod",
        },
        "params": {"cpu-percent": "80"},
        "verification": {
            "level": "partial",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
    }


def test_recover_data_does_not_mix_recover_state_inject_facts():
    wrong_recover_spec = FaultSpec(
        namespace="wrong-ns",
        scope="node",
        names=("wrong-node",),
        fault_target="network",
        fault_action="loss",
    )
    recover_state = {
        "operation": "recover",
        "fault_spec": wrong_recover_spec.to_dict(),
        "experiment_uid": "wrong-uid",
        "verification": {
            "level": "stale-inject-verification",
            "layer1": {"status": "failed"},
            "layer2": {"status": "failed"},
        },
        "result": {"recovered": True, "recovery_level": "recovered"},
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
    )

    assert data["task_state"] == "recovered"
    assert data["fault_type"] == "pod-cpu-fullload"
    assert data["experiment_uid"] == "uid-1"
    assert data["target"]["namespace"] == "arms-prom"
    assert data["target"]["names"] == ["pod-a"]
    assert data["verification"]["level"] == "recovered"
    assert data["verification"]["layer1"]["status"] == "passed"


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
        "experiment_uid": "uid-1",
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
        include_experiment_uid=False,
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
            fault_target="network",
            fault_action="loss",
        ),
        error="internal_error: boom",
    )

    assert data == {
        "task_id": "task-inject",
        "result": "failed",
        "fault_type": "pod-network-loss",
        "targets": [{"name": "pod-a", "namespace": "arms-prom"}],
        "experiment_uid": "",
        "error": "internal_error: boom",
    }


def test_unknown_inject_data_uses_complete_result_card_shape():
    assert build_unknown_inject_data("task-inject", experiment_uid="uid-1") == {
        "task_id": "task-inject",
        "task_state": "unknown",
        "fault_type": "",
        "experiment_uid": "uid-1",
        "injection_method": None,
        "fault_handle": None,
        "duration_ms": 0,
        "fault_spec": {},
        "target": {},
        "params": {},
        "execution_artifacts": [],
        "verification": None,
        "side_effects": None,
        "blast_radius_detail": "",
        "inject_context": "",
        "postmortem": None,
        "issue_report": None,
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
        "experiment_uid": "uid-1",
        "targets": [{"name": "pod-a", "namespace": "arms-prom"}],
        "verification": None,
        "error": "internal_error: boom",
    }


def test_recover_state_helpers_map_failed_and_partial_states():
    assert recover_task_state_from_values({"result": {"recovered": False}}) == "failed"
    assert (
        recover_task_state_from_values(
            {"result": {"recovered": True, "recovery_level": "partial"}}
        )
        == "partial_recovered"
    )
    assert (
        recover_result_label_from_values({"result": {"recovered": False}}) == "failed"
    )
    assert (
        recover_result_label_from_values(
            {"result": {"recovered": True, "recovery_level": "partial"}}
        )
        == "partial"
    )


def test_recover_state_helpers_keep_unverified_distinct_from_failed():
    """"unverified" (destroy issued, observation unavailable) must not be
    flattened to "failed" — that would claim counter-evidence nobody
    observed. Both the result-card task_state and the legacy CLI label
    keep the honest-ignorance vocabulary (aligns with infer_task_state)."""
    values = {"result": {"recovered": False, "recovery_level": "unverified"}}
    assert recover_task_state_from_values(values) == "unverified"
    assert recover_result_label_from_values(values) == "unverified"


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


def test_recover_result_builders_keep_recover_and_inject_lanes_separate():
    """Recover result builders should not project inject facts from recover state."""

    text = (
        PROJECT_ROOT / "src/chaos_agent/agent/result/operation_result.py"
    ).read_text(encoding="utf-8")
    forbidden_snippets = [
        "fault_type_from_state(recover_state)",
        "legacy_target_dict(recover_state)",
        "legacy_params_dict(recover_state)",
        'recover_state.get("blade_uid"',
        "read_inject_verification(recover_state)",
        "read_recover_verification(inject_state)",
    ]

    violations = [snippet for snippet in forbidden_snippets if snippet in text]

    assert violations == []


def test_future_experiment_carrier_pins_without_consumer_changes():
    """Phase-7 T6: a NON-blade_uid experiment handle pins its UID through
    ``build_recovery_handle`` by registration alone.

    The membership check is the handle-owning provider's
    ``has_experiment_uid`` declaration (via ``FaultProviderRegistry
    .is_experiment_handle``), not a ``kind == "blade_uid"`` string branch —
    so a future experiment carrier's UID enters the pinned
    ``{"kind", "value", "experiment_uid"}`` contract with zero consumer
    changes. (The blade-family shape itself — kind="blade_uid" — stays
    locked by the existing recover-data assertions above.)
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    class _FutureExperimentProvider:
        # Minimal surface the registry needs to own a handle kind.
        carrier = "future_exp"
        injection_methods = ("future_method",)
        has_experiment_uid = True
        handle_kind = "my_experiment"

    FaultProviderRegistry.register(_FutureExperimentProvider())
    try:
        state = {"fault_handle": {"kind": "my_experiment", "value": "exp-9"}}
        assert build_recovery_handle(state) == {
            "kind": "my_experiment",
            "value": "exp-9",
            "experiment_uid": "exp-9",
        }
    finally:
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()
