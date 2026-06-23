"""Executable contract for high-risk AgentState field ownership."""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_contract import (
    STATE_FIELD_CONTRACTS,
    STATE_HIGH_RISK_FIELDS,
    STATE_LEGACY_COMPAT_FIELDS,
    StateFieldContract,
)
from chaos_agent.agent.state_lifecycle import iter_state_fields


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src/chaos_agent"

_STATE_LIKE_RECEIVERS = {
    "_psv",
    "_rv",
    "_vals",
    "checkpoint_values",
    "inject_state_values",
    "inject_values",
    "initial",
    "initial_state",
    "pv",
    "source_values",
    "state",
    "state_or_values",
    "state_values",
    "values",
    "values_fin",
}


class _StateReadVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, fields_to_track: set[str]):
        self.path = path
        self.fields_to_track = fields_to_track
        self.reads: list[tuple[str, int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value in self.fields_to_track
            and _is_state_like_receiver(node.func.value)
        ):
            self.reads.append((node.args[0].value, node.lineno, _line(self.path, node.lineno)))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if not isinstance(node.ctx, ast.Load):
            self.generic_visit(node)
            return
        field = _constant_subscript_key(node.slice)
        if field in self.fields_to_track and _is_state_like_receiver(node.value):
            self.reads.append((field, node.lineno, _line(self.path, node.lineno)))
        self.generic_visit(node)


def _constant_subscript_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_state_like_receiver(node: ast.AST) -> bool:
    try:
        text = ast.unparse(node)
    except Exception:
        return False
    return text in _STATE_LIKE_RECEIVERS or text.endswith(".values")


def _line(path: Path, lineno: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""


def _iter_state_reads(fields_to_track: set[str]) -> list[tuple[str, str, int, str]]:
    reads: list[tuple[str, str, int, str]] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _StateReadVisitor(path, fields_to_track)
        visitor.visit(tree)
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        reads.extend((field, rel, lineno, text) for field, lineno, text in visitor.reads)
    return reads


def test_state_field_contract_shape_is_stable():
    """Contract entries are intentionally small and data-only."""

    assert [field.name for field in fields(StateFieldContract)] == [
        "name",
        "group",
        "source_of_truth",
        "canonical_reader",
        "canonical_writer",
        "direct_read_paths",
        "notes",
        "high_risk",
    ]


def test_contract_covers_agent_state_and_legacy_compat_fields():
    """Every runtime field and every known legacy projection has a contract."""

    agent_state_fields = set(AgentState.__annotations__)
    lifecycle_fields = set(iter_state_fields())
    legacy_fields = set(STATE_LEGACY_COMPAT_FIELDS)

    assert agent_state_fields == lifecycle_fields
    assert (agent_state_fields | legacy_fields) - set(STATE_FIELD_CONTRACTS) == set()

    assert STATE_FIELD_CONTRACTS["fault_spec"].canonical_reader == "read_fault_spec"
    assert STATE_FIELD_CONTRACTS["approved_target"].canonical_writer == (
        "freeze_approved_target_from_spec"
    )
    assert STATE_FIELD_CONTRACTS["verification"].canonical_reader == (
        "read_inject_verification"
    )
    assert STATE_FIELD_CONTRACTS["recover_verification"].canonical_reader == (
        "read_recover_verification"
    )
    assert STATE_FIELD_CONTRACTS["result"].canonical_reader == "read_operation_outcome"


def test_enforced_high_risk_state_reads_stay_within_contract_paths():
    """Direct reads of enforced high-risk fields must remain at named boundaries."""

    enforced = {
        field
        for field in STATE_HIGH_RISK_FIELDS
        if STATE_FIELD_CONTRACTS[field].direct_read_paths
    }
    violations: list[str] = []
    for field, rel, lineno, text in _iter_state_reads(enforced):
        allowed = set(STATE_FIELD_CONTRACTS[field].direct_read_paths)
        if rel not in allowed:
            violations.append(f"{rel}:{lineno}: {field}: {text}")

    assert violations == []


def test_legacy_target_and_params_are_compatibility_only():
    """target/params should only be read while rebuilding or projecting FaultSpec."""

    for field in ("target", "params"):
        contract = STATE_FIELD_CONTRACTS[field]
        assert contract.source_of_truth == "fault_spec"
        assert set(contract.direct_read_paths) == {
            "src/chaos_agent/agent/fault_spec.py",
            "src/chaos_agent/agent/task_snapshot.py",
        }


def test_operation_outcome_fields_are_helper_owned():
    """Outcome fields should be consumed through OperationOutcome."""

    for field in ("result", "error", "failure_reason", "failure_detail", "postmortem"):
        contract = STATE_FIELD_CONTRACTS[field]
        assert contract.source_of_truth == "operation_outcome"
        assert contract.canonical_reader == "read_operation_outcome"
        assert contract.direct_read_paths == (
            "src/chaos_agent/agent/operation_outcome.py",
        )


def test_skill_name_is_active_skill_not_reported_fault_type():
    """skill_name is only a skill-routing field; reports use FaultSpec."""

    contract = STATE_FIELD_CONTRACTS["skill_name"]

    assert contract.source_of_truth == "skill_activation"
    assert "read_active_skill_name" in contract.canonical_reader
    assert "fault_type_from_state" in contract.canonical_reader
    assert set(contract.direct_read_paths) == {
        "src/chaos_agent/agent/fault_spec.py",
        "src/chaos_agent/agent/skill_identity.py",
    }
