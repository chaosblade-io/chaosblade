"""Semantic ownership contract for flat AgentState fields.

``state_lifecycle`` answers "when is a field reset?".  This module answers
"who is allowed to treat a field as a source of truth?".

The graph still keeps a flat state shape for LangGraph checkpointing,
TaskStore compatibility and old task records.  New code should use the
semantic helpers named here instead of reading historical top-level fields
directly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from chaos_agent.agent.state_lifecycle import STATE_FIELD_POLICIES


@dataclass(frozen=True)
class StateFieldContract:
    """Access contract for one AgentState or legacy compatibility field."""

    name: str
    group: str
    source_of_truth: str
    canonical_reader: str = ""
    canonical_writer: str = ""
    direct_read_paths: tuple[str, ...] = ()
    notes: str = ""
    high_risk: bool = False


STATE_LEGACY_COMPAT_FIELDS: tuple[str, ...] = (
    "target",
    "params",
    "fault_type",
    "blade_scope",
    "blade_target",
    "blade_action",
    "params_flags",
    "duration",
    "duration_seconds",
)

_FAULT_SPEC_COMPAT_READ_PATHS = (
    "src/chaos_agent/agent/fault_spec.py",
    "src/chaos_agent/agent/task_snapshot.py",
)

_SKILL_NAME_READ_PATHS = (
    "src/chaos_agent/agent/fault_spec.py",
    "src/chaos_agent/agent/skill_identity.py",
)

_OPERATION_OUTCOME_READ_PATHS = (
    "src/chaos_agent/agent/operation_outcome.py",
)

_VERIFICATION_READ_PATHS = (
    "src/chaos_agent/agent/operation_outcome.py",
)


def _default_contracts() -> dict[str, StateFieldContract]:
    contracts = {
        name: StateFieldContract(
            name=name,
            group=policy.group,
            source_of_truth="agent_state",
        )
        for name, policy in STATE_FIELD_POLICIES.items()
    }
    for name in STATE_LEGACY_COMPAT_FIELDS:
        contracts[name] = StateFieldContract(
            name=name,
            group="legacy_compat",
            source_of_truth="fault_spec",
            canonical_reader="read_fault_spec / legacy_*_dict",
            direct_read_paths=_FAULT_SPEC_COMPAT_READ_PATHS,
            notes=(
                "Compatibility projection for old checkpoints and task records. "
                "New code should consume FaultSpec."
            ),
            high_risk=True,
        )
    return contracts


def _with_overrides(
    contracts: dict[str, StateFieldContract],
) -> dict[str, StateFieldContract]:
    contracts["fault_spec"] = replace(
        contracts["fault_spec"],
        source_of_truth="fault_spec",
        canonical_reader="read_fault_spec",
        canonical_writer="FaultSpec.to_dict",
        notes="Canonical fault intent: scope, target identity, action and params.",
        high_risk=True,
    )
    contracts["skill_name"] = replace(
        contracts["skill_name"],
        source_of_truth="skill_activation",
        canonical_reader=(
            "read_active_skill_name for skill routing; "
            "fault_type_from_state for reporting"
        ),
        direct_read_paths=_SKILL_NAME_READ_PATHS,
        notes=(
            "Currently dual-use: skill activation id plus legacy fault type fallback. "
            "Reporting must prefer fault_type_from_state()."
        ),
        high_risk=True,
    )
    contracts["approved_target"] = replace(
        contracts["approved_target"],
        source_of_truth="target_guard",
        canonical_reader="approved_from_dict",
        canonical_writer="freeze_approved_target_from_spec",
        notes="Frozen user-approved target snapshot for tool-call drift checks.",
        high_risk=True,
    )

    for field in ("verification", "recover_verification"):
        contracts[field] = replace(
            contracts[field],
            source_of_truth="operation_outcome",
            canonical_reader=(
                "read_inject_verification"
                if field == "verification"
                else "read_recover_verification"
            ),
            canonical_writer=(
                "write_inject_verification"
                if field == "verification"
                else "write_recover_verification"
            ),
            direct_read_paths=_VERIFICATION_READ_PATHS,
            notes="Physical verification lane. Result/reporting code should use helpers.",
            high_risk=True,
        )

    for field in ("result", "error", "failure_reason", "failure_detail", "postmortem"):
        contracts[field] = replace(
            contracts[field],
            source_of_truth="operation_outcome",
            canonical_reader="read_operation_outcome",
            canonical_writer="write_* helper or fail_state",
            direct_read_paths=_OPERATION_OUTCOME_READ_PATHS,
            notes="Terminal outcome field. Consumers should read OperationOutcome.",
            high_risk=True,
        )

    return contracts


STATE_FIELD_CONTRACTS: dict[str, StateFieldContract] = _with_overrides(
    _default_contracts()
)

STATE_HIGH_RISK_FIELDS: tuple[str, ...] = tuple(
    name for name, contract in STATE_FIELD_CONTRACTS.items() if contract.high_risk
)


def state_field_contract(field: str) -> StateFieldContract | None:
    """Return the semantic contract for an AgentState field."""

    return STATE_FIELD_CONTRACTS.get(field)


__all__ = [
    "STATE_FIELD_CONTRACTS",
    "STATE_HIGH_RISK_FIELDS",
    "STATE_LEGACY_COMPAT_FIELDS",
    "StateFieldContract",
    "state_field_contract",
]
