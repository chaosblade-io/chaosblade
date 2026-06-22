"""Static guards for FaultSpec boundary ownership."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_result_and_finalize_paths_do_not_read_legacy_target_params_directly():
    """Result/session writers should project target/params from fault_spec helpers."""

    forbidden_by_file = {
        "src/chaos_agent/server/routes/inject.py": [
            'values_fin.get("target")',
            'values_fin.get("params")',
        ],
        "src/chaos_agent/server/routes/turn_result.py": [
            'values.get("target")',
            'values.get("params")',
        ],
        "src/chaos_agent/cli/result_builder.py": [
            'values.get("target")',
            'values.get("params")',
        ],
        "src/chaos_agent/cli/session_finalize.py": [
            'values_fin.get("target")',
            'values_fin.get("params")',
        ],
        "src/chaos_agent/agent/nodes/memory_nodes.py": [
            'state.get("target")',
            'state.get("params")',
        ],
    }

    violations = []
    for rel, forbidden_snippets in forbidden_by_file.items():
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in text:
                violations.append(f"{rel}: {snippet}")

    assert violations == []


def test_result_reporting_paths_use_fault_type_projection_helper():
    """Reporting paths should not hand-roll fault_spec → skill_name fallback."""

    checked_files = [
        "src/chaos_agent/agent/state.py",
        "src/chaos_agent/server/routes/turn_result.py",
        "src/chaos_agent/server/routes/turn_event_stream.py",
        "src/chaos_agent/cli/runner.py",
        "src/chaos_agent/agent/nodes/batch_next.py",
        "src/chaos_agent/agent/postmortem/builder.py",
        "src/chaos_agent/agent/experience.py",
    ]
    forbidden_snippets = [
        "spec.fault_type if",
        'else state.get("skill_name"',
        'else values.get("skill_name"',
    ]

    violations = []
    for rel in checked_files:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        if "fault_type_from_state" not in text:
            violations.append(f"{rel}: missing fault_type_from_state")
        for snippet in forbidden_snippets:
            if snippet in text:
                violations.append(f"{rel}: {snippet}")

    assert violations == []


def test_cli_recover_display_projects_params_from_fault_spec_helper():
    """CLI recover display should not read legacy params as source of truth."""

    text = (PROJECT_ROOT / "src/chaos_agent/cli/runner.py").read_text(encoding="utf-8")

    assert 'state_values.get("params")' not in text
    assert "legacy_params_dict(state_values)" in text


def test_recover_snapshot_paths_share_fault_name_parser():
    """Recover rebuild paths should not keep private fault name parsers."""

    checked_files = [
        "src/chaos_agent/agent/recovery_state.py",
        "src/chaos_agent/agent/task_snapshot.py",
    ]

    violations = []
    for rel in checked_files:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        if "def _fault_parts_from_name" in text:
            violations.append(f"{rel}: private _fault_parts_from_name")
        if "fault_parts_from_name" not in text and rel.endswith("task_snapshot.py"):
            violations.append(f"{rel}: missing shared fault_parts_from_name")

    assert violations == []


def test_approved_target_writers_freeze_from_fault_spec():
    """Nodes that write approved_target should not hand-roll legacy pieces."""

    checked_files = [
        "src/chaos_agent/agent/nodes/confirmation_gate.py",
        "src/chaos_agent/agent/nodes/safety_check.py",
        "src/chaos_agent/agent/nodes/tool_screener.py",
    ]

    violations = []
    for rel in checked_files:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        if "freeze_approved_target_from_spec" not in text:
            violations.append(f"{rel}: missing freeze_approved_target_from_spec")
        if "freeze_approved_target(" in text:
            violations.append(f"{rel}: direct freeze_approved_target call")

    assert violations == []


def test_explanation_layers_read_outcome_through_helpers():
    """Postmortem/experience/memory summaries should not read legacy outcome fields directly."""

    forbidden_by_file = {
        "src/chaos_agent/agent/experience.py": [
            'state.get("error"',
            'state.get("failure_detail"',
            'state.get("result"',
            'state.get("skill_name"',
        ],
        "src/chaos_agent/agent/postmortem/builder.py": [
            'state.get("error"',
            'state.get("failure_detail"',
            'state.get("result"',
            'state.get("skill_name"',
        ],
        "src/chaos_agent/agent/nodes/memory_nodes.py": [
            'state.get("error"',
            'state.get("failure_detail"',
            'state.get("result"',
            'state.get("skill_name"',
        ],
        "src/chaos_agent/cli/result_builder.py": [
            'values.get("skill_name"',
            'values.get("error"',
            'values.get("failure_detail"',
            'values.get("result"',
        ],
    }

    violations = []
    for rel, forbidden_snippets in forbidden_by_file.items():
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in text:
                violations.append(f"{rel}: {snippet}")

    assert violations == []
