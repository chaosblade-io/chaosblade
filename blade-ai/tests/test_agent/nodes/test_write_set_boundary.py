"""Tests for the write-set contract surfaces beyond the guard (§3).

Covers tasks §3.1 / §3.2 / §3.6 of write-set-approval-contract:

  - confirmation card renders manifest entries verbatim; absent for
    cases without a manifest
  - the "write_set_boundary" resume signal terminates unattended runs
    with FailureCategory.WRITE_SET_BOUNDARY + machine-readable payload
    (entries verbatim, approved-target summary, interactive re-run
    guidance) — BEFORE any cluster mutation
  - approving the card freezes the extended snapshot (mechanism_entries
    carried into the re-freeze from safety_check's snapshot)
  - B12: CLI drift hard-termination reports DRIFT_TERMINATED (no human
    was consulted); genuine human rejection stays USER_REJECTED
  - result envelope surfaces the boundary payload
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes.gates.confirmation_gate import confirmation_gate
from chaos_agent.agent.nodes.planning.tool_screener import (
    SCREENER_ROUTE_FAIL,
    SCREENER_ROUTE_RETRY,
    tool_screener,
)
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.target_guard import approved_from_dict
from chaos_agent.agent.target_guard.freeze import freeze_approved_target_from_spec
from chaos_agent.agent.target_guard.mechanism_writes import (
    MechanismWriteEntry,
    entries_from_list,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_KUBE_SYSTEM_ENTRIES = (
    MechanismWriteEntry(scope="configmap", namespace="kube-system",
                        names=("coredns-custom",)),
    MechanismWriteEntry(scope="configmap", namespace="kube-system",
                        name_prefix="drill-nxdomain-"),
)


def _state_with_widened_manifest(sample_agent_state):
    """CLI-mode state whose settled case carries a kube-system manifest."""
    state = sample_agent_state
    state["skill_name"] = "pod-network-nxdomain"
    state["safety_status"] = "safe"
    state["interaction_mode"] = "cli"
    state["plan"] = "Patch coredns-custom in kube-system to make the victim's DNS fail."
    replace_spec = FaultSpec(
        scope="pod", namespace="default", names=["victim-pod"],
        fault_target="network", fault_action="loss",
        case_resource_path="cases/NXDOMAIN.md",
    )
    state["fault_spec"] = replace_spec.to_dict()
    state["approved_target"] = freeze_approved_target_from_spec(
        replace_spec, mechanism_entries=_KUBE_SYSTEM_ENTRIES,
    )
    return state


def _state_without_manifest(sample_agent_state):
    """Same victim, no case manifest — the today-shaped control."""
    state = sample_agent_state
    state["skill_name"] = "pod-cpu"
    state["safety_status"] = "safe"
    state["interaction_mode"] = "cli"
    state["plan"] = "CPU fullload on the victim pod."
    spec = FaultSpec(
        scope="pod", namespace="default", names=["victim-pod"],
        fault_target="cpu", fault_action="fullload",
    )
    state["fault_spec"] = spec.to_dict()
    state["approved_target"] = freeze_approved_target_from_spec(spec)
    return state


def _ai_with_tool_call(name: str, args: dict, call_id: str = "tc-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


# ---------------------------------------------------------------------------
# §3.1 Card payload
# ---------------------------------------------------------------------------

class TestCardPayload:
    @pytest.mark.asyncio
    async def test_card_renders_manifest_entries_verbatim(
        self, sample_agent_state,
    ):
        state = _state_with_widened_manifest(sample_agent_state)
        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="approved",
        ) as mock_interrupt:
            await confirmation_gate(state)
        payload = mock_interrupt.call_args[0][0]
        entries = payload["mechanism_writes"]
        assert entries[0]["scope"] == "configmap"
        assert entries[0]["namespace"] == "kube-system"
        assert entries[0]["names"] == ["coredns-custom"]
        assert entries[1]["name_prefix"] == "drill-nxdomain-"
        # Boundary marker for the unattended runner.
        assert payload["write_set_widened"]["mechanism_writes"] == entries

    @pytest.mark.asyncio
    async def test_card_without_manifest_unchanged(self, sample_agent_state):
        state = _state_without_manifest(sample_agent_state)
        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="approved",
        ) as mock_interrupt:
            await confirmation_gate(state)
        payload = mock_interrupt.call_args[0][0]
        assert "mechanism_writes" not in payload
        assert "write_set_widened" not in payload

    @pytest.mark.asyncio
    async def test_victim_covered_entries_do_not_widen(self, sample_agent_state):
        # Node entry under a pod victim: the secondary net already covers
        # the domain — not widening, no marker (unattended keeps semantics).
        state = _state_without_manifest(sample_agent_state)
        spec = FaultSpec.from_dict(state["fault_spec"])
        state["approved_target"] = freeze_approved_target_from_spec(
            spec,
            mechanism_entries=(
                MechanismWriteEntry(scope="node", namespace="", names=("worker-1",)),
            ),
        )
        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="approved",
        ) as mock_interrupt:
            await confirmation_gate(state)
        payload = mock_interrupt.call_args[0][0]
        assert "write_set_widened" not in payload


# ---------------------------------------------------------------------------
# §3.2 Unattended boundary exit
# ---------------------------------------------------------------------------

class TestUnattendedBoundaryExit:
    @pytest.mark.asyncio
    async def test_boundary_resume_terminates_before_execution(
        self, sample_agent_state,
    ):
        state = _state_with_widened_manifest(sample_agent_state)
        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="write_set_boundary",
        ):
            result = await confirmation_gate(state)

        assert result["safety_status"] == "rejected"
        assert result["needs_confirmation"] is False
        # Dedicated category, distinct from USER_REJECTED.
        assert result["failure_detail"]["category"] == "write_set_boundary"
        # Stale approval cleared: nothing downstream may mutate.
        assert result["approved_target"] is None
        # Machine-readable payload: entries verbatim + guidance.
        payload = result["write_set_boundary"]
        assert payload["type"] == "write_set_boundary"
        assert payload["mechanism_writes"][0]["names"] == ["coredns-custom"]
        assert payload["mechanism_writes"][1]["name_prefix"] == "drill-nxdomain-"
        assert payload["approved_target"]["scope"] == "pod"
        assert payload["approved_target"]["namespace"] == "default"
        guidance = payload["guidance"]
        assert "interactive" in guidance
        assert "--confirm" in guidance or "TUI" in guidance

    @pytest.mark.asyncio
    async def test_human_approval_freezes_extended_contract(
        self, sample_agent_state,
    ):
        # Interactive channel (TUI card / CLI confirm callback) approves:
        # the freeze carries the manifest entries — the extended contract.
        state = _state_with_widened_manifest(sample_agent_state)
        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="approved",
        ):
            result = await confirmation_gate(state)
        assert result["needs_confirmation"] is False
        frozen = result["approved_target"]
        assert entries_from_list(frozen["mechanism_entries"]) == _KUBE_SYSTEM_ENTRIES
        hydrated = approved_from_dict(frozen)
        assert len(hydrated.mechanism_entries) == 2

    @pytest.mark.asyncio
    async def test_genuine_human_rejection_stays_user_rejected(
        self, sample_agent_state,
    ):
        state = _state_with_widened_manifest(sample_agent_state)
        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="rejected",
        ):
            result = await confirmation_gate(state)
        assert result["failure_detail"]["category"] == "user_rejected"
        assert result["approved_target"] is None


# ---------------------------------------------------------------------------
# §3.3 / §3.4 Category honesty (B12)
# ---------------------------------------------------------------------------

class TestDriftTerminationCategory:
    @pytest.mark.asyncio
    async def test_cli_second_drift_reports_drift_terminated(self):
        from chaos_agent.config.settings import settings
        # Snapshot-restore: hard-coding False poisoned later suites that
        # rely on the session default (enforcing=True).
        orig_enforcing = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            state = {
                "interaction_mode": "cli",
                "messages": [
                    _ai_with_tool_call("blade_create", {
                        "scope": "pod", "target": "cpu", "namespace": "ns",
                        "names": ["pod-OTHER"],
                    }),
                ],
                "approved_target": freeze_approved_target_from_spec(
                    FaultSpec(scope="pod", namespace="ns", names=["pod-a"],
                              fault_target="cpu", fault_action="fullload"),
                ),
                "drift_reject_count": 1,
            }
            delta = await tool_screener(state)
            # W-56-5 (defect a): hard stop routes FAIL → reject terminal.
            assert delta["screener_route"] == SCREENER_ROUTE_FAIL
            assert delta["failure_detail"]["category"] == "drift_terminated"
            assert "human" in delta["error"]
        finally:
            settings.target_guard_enforcing = orig_enforcing

    @pytest.mark.asyncio
    async def test_tui_second_drift_keeps_user_rejected(self):
        # TUI: a human really did reject the drift card before this
        # second drift — USER_REJECTED is the honest attribution.
        from chaos_agent.config.settings import settings
        orig_enforcing = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            state = {
                "interaction_mode": "tui",
                "messages": [
                    _ai_with_tool_call("blade_create", {
                        "scope": "pod", "target": "cpu", "namespace": "ns",
                        "names": ["pod-OTHER"],
                    }),
                ],
                "approved_target": freeze_approved_target_from_spec(
                    FaultSpec(scope="pod", namespace="ns", names=["pod-a"],
                              fault_target="cpu", fault_action="fullload"),
                ),
                "drift_reject_count": 1,
            }
            delta = await tool_screener(state)
            assert delta["failure_detail"]["category"] == "user_rejected"
        finally:
            settings.target_guard_enforcing = orig_enforcing


# ---------------------------------------------------------------------------
# §3.5 Result envelope
# ---------------------------------------------------------------------------

class TestUnattendedResumeValue:
    """The runner's unattended auto-approve decision (AUTO delegation)."""

    def test_widened_payload_resumes_approved(self):
        # AUTO delegation (flipped 2026-09-01): the manifest is the
        # authority, the guard enforces the per-name boundary — a widened
        # contract proceeds unattended too, with an auditable
        # ``auto_approved`` event.
        from chaos_agent.cli.runner import _unattended_resume_value
        assert _unattended_resume_value({
            "write_set_widened": {"mechanism_writes": ["..."]},
        }) == "approved"

    def test_normal_payload_resumes_approved(self):
        from chaos_agent.cli.runner import _unattended_resume_value
        assert _unattended_resume_value({"skill_name": "pod-cpu"}) == "approved"
        assert _unattended_resume_value("some string payload") == "approved"
        assert _unattended_resume_value(None) == "approved"


class TestCascadeCarryForward:
    """§2.4 / §4.5: a drift-correction rebuild carries mechanism_entries."""

    @pytest.mark.asyncio
    async def test_drift_correction_preserves_frozen_entries(self):
        # Same-kind drift (pod-a → pod-OTHER) approved after the write set
        # was frozen: the rebuilt snapshot carries mechanism_entries
        # forward, so mechanism writes stay in-contract.
        from chaos_agent.config.settings import settings
        orig_enforcing = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            spec = FaultSpec(
                scope="pod", namespace="ns", names=["pod-a"],
                fault_target="cpu", fault_action="fullload",
            )
            state = {
                "messages": [
                    _ai_with_tool_call("blade_create", {
                        "scope": "pod", "target": "cpu", "namespace": "ns",
                        "names": ["pod-OTHER"],
                    }),
                ],
                "approved_target": freeze_approved_target_from_spec(
                    spec, mechanism_entries=_KUBE_SYSTEM_ENTRIES,
                ),
                "fault_spec": spec.to_dict(),
            }
            with patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="approved",
            ):
                delta = await tool_screener(state)
            assert delta["screener_route"] == "pass"
            rebuilt = delta["approved_target"]
            assert rebuilt["names"] == ["pod-OTHER"]
            assert entries_from_list(rebuilt["mechanism_entries"]) == _KUBE_SYSTEM_ENTRIES
        finally:
            settings.target_guard_enforcing = orig_enforcing


class TestResultEnvelope:
    def test_boundary_payload_surfaces_in_result_data(self):
        from chaos_agent.agent.result.operation_result import (
            build_inject_data_from_state,
        )
        values = {
            "fault_spec": FaultSpec(
                scope="pod", namespace="default", names=["victim-pod"],
            ).to_dict(),
            "write_set_boundary": {
                "type": "write_set_boundary",
                "mechanism_writes": [{"scope": "configmap",
                                      "namespace": "kube-system",
                                      "names": ["coredns-custom"]}],
                "guidance": "re-run interactively",
            },
            "messages": [],
        }
        data = build_inject_data_from_state(values, "task-1")
        assert data["write_set_boundary"]["type"] == "write_set_boundary"
        assert data["write_set_boundary"]["mechanism_writes"][0]["names"] == [
            "coredns-custom",
        ]

    def test_normal_run_has_no_boundary_payload(self):
        from chaos_agent.agent.result.operation_result import (
            build_inject_data_from_state,
        )
        values = {
            "fault_spec": FaultSpec(
                scope="pod", namespace="default", names=["victim-pod"],
            ).to_dict(),
            "messages": [],
        }
        data = build_inject_data_from_state(values, "task-1")
        assert "write_set_boundary" not in data

    def test_new_categories_exist(self):
        assert FailureCategory.WRITE_SET_BOUNDARY.value == "write_set_boundary"
        assert FailureCategory.DRIFT_TERMINATED.value == "drift_terminated"
