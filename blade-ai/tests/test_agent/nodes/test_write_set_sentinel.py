"""Structural tests for the write-set boundary surfaces (D4, AUTO
delegation semantics — flipped 2026-09-01).

The write-set contract's AUTHORITY is the case manifest (legislated
before the run, loaded deterministically, invisible to the LLM); its
ENFORCEMENT is the target_guard's per-name union (fail-closed beyond
the manifest). What this suite pins:

  Layer 1 — visibility + consistency, not gatekeeping:
    - safety_check stamps ``widening_pending_approval`` at freeze time
    - the gate's approved branch is the single clear point (the
      re-frozen snapshot drops the key, manifest entries stay) — the
      unattended AUTO-delegation ("approved") flows through it too
    - a human-approved drift-correction rebuild also clears it
    - execute_loop's entry sentinel AUDITS a still-pending snapshot
      (ERROR log — no approval path ran) and lets execution proceed:
      the guard is the boundary, fail-closing here would trade real
      automation for no real safety
  Layer 2 — safety_check forces ``needs_confirmation=True`` when the
    manifest widens, so the run routes through the gate: interactive
    channels render the card (a human is present, the entries are
    worth one glance), unattended channels take the audited
    auto-approve
  Layer 3 — force_override stays a same-action overlay exemption
    (never a widened-contract authority); the shared
    ``unattended_resume_value`` decides through ONE function for every
    unattended channel — AUTO delegation: always "approved", widened
    or not, with ``widened_auto_approval_payload`` marking the
    auditable event
  Layer 4 — the knowing human actually sees the entries when one is
    present: the shared display formatter, the SSE ``content`` field,
    and the auto-approve audit token all render them
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes.gates._write_set_boundary import (
    execute_loop_entry_sentinel,
    snapshot_widening_pending,
    unattended_resume_value,
    widened_auto_approval_payload,
)
from chaos_agent.agent.nodes.gates.confirmation_gate import confirmation_gate
from chaos_agent.agent.nodes.gates.safety_check import safety_check
from chaos_agent.agent.nodes.planning.tool_screener import tool_screener
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.target_guard.freeze import freeze_approved_target_from_spec
from chaos_agent.agent.target_guard.mechanism_writes import (
    MechanismWriteEntry,
)
from chaos_agent.config.settings import settings


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_KUBE_SYSTEM_ENTRIES = (
    MechanismWriteEntry(scope="configmap", namespace="kube-system",
                        names=("coredns-custom",)),
    MechanismWriteEntry(scope="configmap", namespace="kube-system",
                        name_prefix="drill-nxdomain-"),
)

_WIDENED_CASE = """---
mechanism_writes:
  - scope: configmap
    namespace: kube-system
    names: [coredns-custom]
  - scope: configmap
    namespace: kube-system
    name_prefix: drill-nxdomain-
---
**用例名称** widened fixture

The mechanism writes into kube-system while the victim pod runs in
default — the widened-contract shape.
"""

_COVERED_CASE = """---
mechanism_writes:
  - scope: node
    namespace: ""
    names: [worker-1]
---
**用例名称** covered fixture

The node entry sits inside the pod victim's secondary net — not a
widening, unattended keeps today's semantics.
"""


def _widened_spec() -> FaultSpec:
    return FaultSpec(
        scope="pod", namespace="default", names=["victim-pod"],
        fault_target="network", fault_action="loss",
        case_resource_path="cases/widened.md",
    )


def _pending_snapshot() -> dict:
    """The snapshot shape safety_check produces for a widened case."""
    return freeze_approved_target_from_spec(
        _widened_spec(),
        mechanism_entries=_KUBE_SYSTEM_ENTRIES,
        widening_pending_approval=True,
    )


def _ai_with_tool_call(name: str, args: dict, call_id: str = "tc-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


# ---------------------------------------------------------------------------
# Layer 1 — execute_loop entry sentinel
# ---------------------------------------------------------------------------

class TestExecuteLoopEntrySentinel:
    def test_pending_snapshot_audits_loudly_and_proceeds(
        self, sample_agent_state, caplog,
    ):
        """The sentinel is an assertion, not a gate: a still-pending
        snapshot at execution time means no approval path ran (channel
        bug / raw state write / replan seam) — under AUTO delegation
        that is not a safety violation (the guard enforces the
        manifest boundary per-name), so the sentinel ERROR-logs for
        path investigation and returns None to let execution proceed."""
        import logging as _logging
        state = sample_agent_state
        state["approved_target"] = _pending_snapshot()

        with caplog.at_level(_logging.ERROR, logger="chaos_agent.agent.nodes.gates._write_set_boundary"):
            assert execute_loop_entry_sentinel(state) is None

        assert any(
            "sentinel (audit)" in rec.message
            for rec in caplog.records
        )
        assert any("no approval path ran" in rec.getMessage() for rec in caplog.records)

    def test_cleared_snapshot_passes_sentinel(self, sample_agent_state):
        # Human approved at the gate: the re-frozen snapshot carries no
        # pending marker and the sentinel lets execution proceed.
        state = sample_agent_state
        state["approved_target"] = freeze_approved_target_from_spec(
            _widened_spec(), mechanism_entries=_KUBE_SYSTEM_ENTRIES,
        )
        assert execute_loop_entry_sentinel(state) is None

    def test_legacy_snapshot_hydrates_false(self, sample_agent_state):
        # Snapshots frozen before the marker existed keep their recorded
        # semantics (hydrate as False) — no retroactive termination.
        state = sample_agent_state
        legacy = freeze_approved_target_from_spec(
            _widened_spec(), mechanism_entries=_KUBE_SYSTEM_ENTRIES,
        )
        legacy.pop("widening_pending_approval", None)
        state["approved_target"] = legacy
        assert execute_loop_entry_sentinel(state) is None

    def test_no_snapshot_returns_none(self, sample_agent_state):
        state = sample_agent_state
        state["approved_target"] = None
        assert execute_loop_entry_sentinel(state) is None

    def test_snapshot_widening_pending_reader(self, sample_agent_state):
        state = sample_agent_state
        state["approved_target"] = _pending_snapshot()
        assert snapshot_widening_pending(state) is True
        state["approved_target"] = freeze_approved_target_from_spec(
            _widened_spec(), mechanism_entries=_KUBE_SYSTEM_ENTRIES,
        )
        assert snapshot_widening_pending(state) is False
        state["approved_target"] = None
        assert snapshot_widening_pending(state) is False
        assert snapshot_widening_pending({}) is False


# ---------------------------------------------------------------------------
# Layer 1 + 2 — safety_check stamps the marker and forces the gate route
# ---------------------------------------------------------------------------

class TestSafetyCheckStampsPending:
    @pytest.mark.asyncio
    async def test_widened_manifest_freezes_pending_and_forces_confirmation(
        self, sample_agent_state, tmp_path, monkeypatch,
    ):
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "cases").mkdir(parents=True)
        (skill_dir / "cases" / "widened.md").write_text(
            _WIDENED_CASE, encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")

        state = sample_agent_state
        state["skill_name"] = "demo-skill"
        state["target"] = {"namespace": "default", "names": ["victim-pod"]}
        state["fault_spec"] = _widened_spec().to_dict()
        state["needs_confirmation"] = False

        result = await safety_check(state)

        # Layer 1: the frozen snapshot awaits its knowing human.
        frozen = result["approved_target"]
        assert frozen["widening_pending_approval"] is True
        assert frozen["mechanism_entries"]
        # Layer 2: the confirm route is forced — the ``safe +
        # needs_confirmation=False`` auto-execute route cannot skip the
        # gate, interactive and unattended alike.
        assert result["needs_confirmation"] is True

    @pytest.mark.asyncio
    async def test_no_manifest_snapshot_has_no_pending_key(
        self, sample_agent_state, monkeypatch,
    ):
        # Byte-identical contract: a case without a manifest freezes the
        # same snapshot as before the marker existed, and an unattended
        # run keeps its auto-execute semantics.
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default", "names": ["my-pod"]}
        state["needs_confirmation"] = False

        result = await safety_check(state)

        assert "widening_pending_approval" not in result["approved_target"]
        # No forced key at all — the route keeps its caller's default.
        assert "needs_confirmation" not in result

    @pytest.mark.asyncio
    async def test_victim_covered_manifest_not_pending(
        self, sample_agent_state, tmp_path, monkeypatch,
    ):
        # A manifest whose entries stay inside the victim's secondary
        # net does NOT widen the contract — the gate route is not
        # forced, the snapshot carries no marker.
        skill_dir = tmp_path / "demo-skill"
        (skill_dir / "cases").mkdir(parents=True)
        (skill_dir / "cases" / "covered.md").write_text(
            _COVERED_CASE, encoding="utf-8",
        )
        monkeypatch.setattr(
            "chaos_agent.skills.loader.get_skills_dir", lambda: tmp_path,
        )
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")

        state = sample_agent_state
        state["skill_name"] = "demo-skill"
        state["target"] = {"namespace": "default", "names": ["victim-pod"]}
        state["fault_spec"] = FaultSpec(
            scope="pod", namespace="default", names=["victim-pod"],
            fault_target="network", fault_action="loss",
            case_resource_path="cases/covered.md",
        ).to_dict()
        state["needs_confirmation"] = False

        result = await safety_check(state)

        assert "widening_pending_approval" not in result["approved_target"]
        # No forced key at all — the route keeps its caller's default.
        assert "needs_confirmation" not in result


# ---------------------------------------------------------------------------
# Layer 1 — the gate's approved branch is the single clear point
# ---------------------------------------------------------------------------

class TestGateApprovedClearsMarker:
    @pytest.mark.asyncio
    async def test_approved_refreeze_drops_pending_key(
        self, sample_agent_state,
    ):
        state = sample_agent_state
        state["skill_name"] = "pod-network-nxdomain"
        state["safety_status"] = "safe"
        state["interaction_mode"] = "cli"
        state["plan"] = "Patch coredns-custom in kube-system."
        state["fault_spec"] = _widened_spec().to_dict()
        state["approved_target"] = _pending_snapshot()
        state["needs_confirmation"] = True

        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="approved",
        ):
            result = await confirmation_gate(state)

        assert result["needs_confirmation"] is False
        frozen = result["approved_target"]
        # The extended contract survives (entries kept)…
        assert frozen["mechanism_entries"]
        # …but the snapshot is now executable — no pending marker.
        assert "widening_pending_approval" not in frozen
        # And the sentinel agrees.
        assert snapshot_widening_pending({"approved_target": frozen}) is False

    @pytest.mark.asyncio
    async def test_drift_rebuild_clears_marker(self):
        # A human approved the drift correction: the rebuilt snapshot is
        # a human-approved shape, so it must not carry the marker — the
        # flag's meaning is "the interactive card never fired", and a
        # drift-approved rebuild DID fire one (keeping the marker would
        # leave a standing false audit signal).
        orig_enforcing = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        try:
            state = {
                "interaction_mode": "tui",
                "messages": [
                    _ai_with_tool_call("blade_create", {
                        "scope": "pod", "target": "network",
                        "namespace": "default",
                        "names": ["victim-OTHER"],
                    }),
                ],
                "approved_target": _pending_snapshot(),
                "fault_spec": _widened_spec().to_dict(),
            }
            with patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="approved",
            ):
                delta = await tool_screener(state)
            rebuilt = delta["approved_target"]
            assert rebuilt["names"] == ["victim-OTHER"]
            assert rebuilt["mechanism_entries"]
            assert "widening_pending_approval" not in rebuilt
        finally:
            settings.target_guard_enforcing = orig_enforcing


# ---------------------------------------------------------------------------
# Layer 3 — force_override narrowing + shared unattended decision
# ---------------------------------------------------------------------------

class TestForceOverrideNarrowed:
    @pytest.mark.asyncio
    async def test_force_override_cannot_bypass_widened_contract(
        self, sample_agent_state,
    ):
        """--force-override exempts the same-action overlay warning; it
        is no widened-contract authority. In an interactive session the
        card must still fire — a human is present, the manifest entries
        are worth one glance."""
        state = sample_agent_state
        state["skill_name"] = "pod-network-nxdomain"
        state["safety_status"] = "confirm_required"
        state["interaction_mode"] = "tui"
        state["force_override"] = True
        state["plan"] = "Patch coredns-custom in kube-system."
        state["fault_spec"] = _widened_spec().to_dict()
        state["approved_target"] = _pending_snapshot()

        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="approved",
        ) as mock_interrupt:
            await confirmation_gate(state)

        assert mock_interrupt.call_count == 1
        payload = mock_interrupt.call_args[0][0]
        assert payload["mechanism_writes"]

    @pytest.mark.asyncio
    async def test_cli_confirm_required_widened_not_short_circuited(
        self, sample_agent_state,
    ):
        """The CLI confirm_required short-circuit ("Add
        --force-override") must not swallow a widened contract: under
        AUTO delegation the widened contract proceeds WITHOUT any flag
        (the manifest is the authority), so that guidance is misleading
        friction. Widened payloads reach the interrupt, where the
        unattended runner resumes "approved" through the shared helper
        — the gate's approved branch clears the pending marker, and the
        snapshot is executable (the sentinel agrees)."""
        state = sample_agent_state
        state["skill_name"] = "pod-network-nxdomain"
        state["safety_status"] = "confirm_required"
        state["interaction_mode"] = "cli"
        state["plan"] = "Patch coredns-custom in kube-system."
        state["fault_spec"] = _widened_spec().to_dict()
        state["approved_target"] = _pending_snapshot()

        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="approved",
        ) as mock_interrupt:
            result = await confirmation_gate(state)

        # The interrupt fired — the unattended runner decides through
        # the shared helper, not the force-override guidance.
        assert mock_interrupt.call_count == 1
        payload = mock_interrupt.call_args[0][0]
        assert payload["write_set_widened"]["mechanism_writes"]
        # The AUTO-delegation resume ("approved") flows through the
        # gate's approved branch: marker cleared, snapshot executable.
        assert result["needs_confirmation"] is False
        frozen = result["approved_target"]
        assert "widening_pending_approval" not in frozen
        assert snapshot_widening_pending({"approved_target": frozen}) is False

    @pytest.mark.asyncio
    async def test_force_override_still_bypasses_plain_confirm_required(
        self, sample_agent_state,
    ):
        # Control: without a manifest the flag keeps its charter — no
        # card, straight to approval (existing behaviour unchanged).
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["safety_status"] = "confirm_required"
        state["interaction_mode"] = "cli"
        state["force_override"] = True
        state["target"] = {"namespace": "default", "names": ["my-pod"]}

        with patch(
            "chaos_agent.agent.nodes.gates.confirmation_gate.interrupt",
            return_value="approved",
        ) as mock_interrupt:
            result = await confirmation_gate(state)

        assert mock_interrupt.call_count == 0
        assert result["needs_confirmation"] is False


class TestUnattendedResumeShared:
    """The single source every unattended channel must consult."""

    def test_widened_payload_resumes_approved(self):
        # AUTO delegation: the manifest is the authority (legislated
        # before the run, invisible to the LLM) and the guard enforces
        # the per-name boundary — a widened contract proceeds unattended
        # too, same as any other payload.
        assert unattended_resume_value({
            "write_set_widened": {"mechanism_writes": [{"scope": "x"}]},
        }) == "approved"

    def test_widened_auto_approval_payload_marks_the_event(self):
        # The delegation is AUDITABLE: a widened payload is returned
        # verbatim (channels emit ``auto_approved`` with it); ordinary
        # payloads yield None — no event, no noise.
        widened = {
            "write_set_widened": {"mechanism_writes": [{"scope": "x"}]},
        }
        assert widened_auto_approval_payload(widened) is widened
        assert widened_auto_approval_payload({"skill_name": "pod-cpu"}) is None
        assert widened_auto_approval_payload("string payload") is None
        assert widened_auto_approval_payload(None) is None

    def test_normal_payload_resumes_approved(self):
        assert unattended_resume_value({"skill_name": "pod-cpu"}) == "approved"
        assert unattended_resume_value("string payload") == "approved"
        assert unattended_resume_value(None) == "approved"

    def test_runner_reexports_shared_decision(self):
        # The runner-level alias must stay the SAME function — channels
        # deciding through anything else are the bug this change fixed.
        # (L4 resolves it through a function-local import, so it has no
        # module-level attribute; its call site is converged by
        # construction.)
        from chaos_agent.cli import runner
        assert runner._unattended_resume_value is unattended_resume_value


# ---------------------------------------------------------------------------
# Layer 4 — the knowing human actually sees the entries
# ---------------------------------------------------------------------------

class TestInformedSurfaces:
    def test_format_mechanism_writes_for_display(self):
        from chaos_agent.agent.target_guard.mechanism_writes import (
            format_mechanism_writes_for_display,
        )
        # Empty input renders "" — manifest-free cards untouched.
        assert format_mechanism_writes_for_display([]) == ""
        assert format_mechanism_writes_for_display(None) == ""
        block = format_mechanism_writes_for_display([
            {"scope": "configmap", "namespace": "kube-system",
             "names": ["coredns"], "name_prefix": "", "description": ""},
            {"scope": "configmap", "namespace": "kube-system",
             "names": [], "name_prefix": "drill-", "description": ""},
        ])
        assert "configmap/kube-system: coredns" in block
        assert "'drill-' (prefix)" in block

    def test_content_from_interrupt_payload_appends_entries(self):
        from chaos_agent.server.routes.turn_interrupt import (
            content_from_interrupt_payload,
        )
        content = content_from_interrupt_payload({
            "summary": "Confirm before execution",
            "mechanism_writes": [
                {"scope": "configmap", "namespace": "kube-system",
                 "names": ["coredns"], "name_prefix": "", "description": ""},
            ],
        })
        assert "Confirm before execution" in content
        assert "configmap/kube-system: coredns" in content

    def test_auto_approve_token_appends_entries(self):
        from chaos_agent.server.routes.turn_interrupt import (
            format_auto_approve_info,
        )
        token = format_auto_approve_info("confirmation_gate", {
            "fault_intent": {"fault_type": "pod-network-loss"},
            "target": {"namespace": "default", "names": ["victim-pod"]},
            "safety_status": "safe",
            "mechanism_writes": [
                {"scope": "configmap", "namespace": "kube-system",
                 "names": ["coredns"], "name_prefix": "", "description": ""},
            ],
        })
        assert "[Auto-approved: confirmation_gate]" in token
        # The delegation stays auditable: the entries ride the token.
        assert "configmap/kube-system: coredns" in token
