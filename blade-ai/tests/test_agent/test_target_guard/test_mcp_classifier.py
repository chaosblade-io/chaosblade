"""Tests for the MCP-tool branch of ``infer_effective_target``.

Posture A (trust ``attach_to``): an operator-attached MCP tool is
classified READONLY so the read-only phase screens (phase1 / verifier /
recover Layer2) stop rejecting it as UNKNOWN, which is what lets an
observation MCP server substitute for kubectl when RBAC is missing.

These tests pin:
  - registered MCP tools → SCOPE_READONLY (both read-only and phase2 attach);
  - unregistered / empty-registry → the pre-existing default-deny is
    byte-identical (backward compatibility, no regression for direct
    classifier callers);
  - the READONLY verdict integrates with ``find_readonly_violations``
    (the tool is NOT a read-only violation);
  - write-set projection is not polluted (READONLY is excluded from the
    approved write-set, per the shared-classifier invariant).
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.nodes._readonly_screen import find_readonly_violations
from chaos_agent.agent.target_guard.classifier import (
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.guard import target_drift_guard
from chaos_agent.agent.target_guard.types import GuardVerdict
from chaos_agent.mcp.registry import McpToolRegistry


@pytest.fixture(autouse=True)
def _clean_registry():
    McpToolRegistry.clear()
    yield
    McpToolRegistry.clear()


class TestMcpClassification:
    @pytest.mark.parametrize(
        "attach_to",
        [
            ("phase1",),
            ("verifier",),
            ("clarification",),
            ("phase1", "verifier"),
        ],
    )
    def test_readonly_phase_attach_is_readonly(self, attach_to):
        McpToolRegistry.register("prom__query_range", attach_to)
        eff = infer_effective_target("prom__query_range", {"q": "up"})
        assert eff.scope == SCOPE_READONLY

    def test_phase2_attach_is_readonly_passthrough(self):
        """Posture A: even a phase2-attached MCP tool passes through
        READONLY (best-effort; the operator owns the wiring). The verdict
        is READONLY, the WARNING audit log is asserted separately."""
        McpToolRegistry.register("mut__apply", ("phase2",))
        eff = infer_effective_target("mut__apply", {"x": 1})
        assert eff.scope == SCOPE_READONLY

    def test_raw_command_preserved(self):
        McpToolRegistry.register("prom__query", ("phase1",))
        eff = infer_effective_target("prom__query", {"q": "up"})
        assert eff.raw_command  # audit rendering always populated

    def test_unregistered_tool_still_default_deny(self):
        """An MCP-shaped name that is NOT in the registry keeps the
        pre-existing default-deny — the branch is a pure addition."""
        eff = infer_effective_target("prom__query", {"q": "up"})
        assert eff.scope == SCOPE_UNKNOWN

    def test_empty_registry_default_deny_unchanged(self):
        """Direct classifier callers (unit tests, MCP disabled) see the
        byte-identical UNKNOWN verdict for unknown tools."""
        eff = infer_effective_target("some_brand_new_tool", {})
        assert eff.scope == SCOPE_UNKNOWN

    def test_known_readonly_tool_unaffected(self):
        """The MCP branch sits AFTER the built-in read-only whitelist and
        the provider dispatch, so those verdicts are untouched."""
        eff = infer_effective_target("time_wait", {"seconds": 5})
        assert eff.scope == SCOPE_READONLY


class TestReadOnlyScreenIntegration:
    def test_registered_mcp_tool_not_a_readonly_violation(self):
        McpToolRegistry.register("prom__query", ("phase1", "verifier"))
        calls = [{"name": "prom__query", "args": {"q": "up"}, "id": "c1"}]
        assert find_readonly_violations(calls) == []

    def test_unregistered_tool_is_a_readonly_violation(self):
        """Regression anchor: without registration the read-only screen
        still rejects the unknown tool (the bug this change fixes)."""
        calls = [{"name": "prom__query", "args": {"q": "up"}, "id": "c1"}]
        violations = find_readonly_violations(calls)
        assert len(violations) == 1
        assert violations[0][0] == "prom__query"


class TestWriteSetProjectionNotPolluted:
    def test_readonly_verdict_contributes_no_write(self):
        """The classifier is shared with write-set projection. A READONLY
        verdict means 'no mutation', so an MCP tool never enters the
        approved write-set — the shared-classifier invariant holds."""
        McpToolRegistry.register("prom__query", ("phase1",))
        eff = infer_effective_target("prom__query", {"q": "up"})
        assert eff.scope == SCOPE_READONLY
        # READONLY carries no target identity to project into a write-set.
        assert eff.names == ()
        assert eff.fault_target == ""
        assert eff.namespace == ""


class TestPhase2DriftGuard:
    """phase2 tool_screener routes an EffectiveTarget through
    ``target_drift_guard``. A READONLY verdict is step 1 — it short-circuits
    BEFORE the ``approved is None`` default-deny and the drift comparison,
    so an MCP tool passes phase2 regardless of approval state."""

    def test_mcp_readonly_passes_drift_guard_without_approval(self):
        McpToolRegistry.register("prom__query", ("phase2",))
        eff = infer_effective_target("prom__query", {"q": "up"})
        decision = target_drift_guard(eff, None)
        assert decision.verdict == GuardVerdict.READONLY

    def test_mcp_readonly_passes_drift_guard_with_approval(self):
        from chaos_agent.agent.target_guard.types import ApprovedTarget

        McpToolRegistry.register("prom__query", ("phase1", "verifier"))
        eff = infer_effective_target("prom__query", {"q": "up"})
        approved = ApprovedTarget(scope="pod", namespace="default", names=("x",))
        decision = target_drift_guard(eff, approved)
        assert decision.verdict == GuardVerdict.READONLY
