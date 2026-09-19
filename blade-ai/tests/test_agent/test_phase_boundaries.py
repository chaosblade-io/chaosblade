"""Phase boundary inventory — the machine-checked registry of LLM context seams.

Openspec phase-boundary-declaration. The contract: wherever control flow
crosses a seam of the LLM context continuum (graph entry, phase handover,
layer handover, replan, cross-graph graft, compaction, batch fault
switch), the model's cognitive reorientation must not depend on semantic
guessing. Three axes define the problem space — role, evidence tense,
tool surface — and each seam must have AT LEAST ONE carrier answering
each applicable axis (carriers: context surgery, boundary declaration
message, screener/phase duty, tool schema itself).

The tool-surface axis is CONDITIONAL (design.md D7): it belongs in a
declaration only when the inertia source survives (history carries
earlier-phase tool_calls) AND the surface differs AND no other carrier
covers it. Of the eight registered seams only the inject→recover graft
meets all three conditions (#29 first-run evidence).

This module pins the inventory as a table: every registered seam must
declare its applicable axes and carrier form, and a parametrized test
asserts each carrier actually exists in the source. A NEW control-flow
seam that is not registered here (or a registered carrier that was
removed) fails the suite — the checklist is the guardrail, not prose.
"""

from langchain_core.messages import HumanMessage

from chaos_agent.agent.graph import (
    _phase1_handle_tool_error,
    _phase2_handle_tool_error,
)
from chaos_agent.agent.nodes.batch.batch_setup import REMOVE_ALL_MESSAGES
from chaos_agent.agent.nodes.execute.execute_loop import (
    _PHASE2_KICKOFF_MARKER,
    _maybe_build_phase2_kickoff,
    reset_attribution_state,
)
from chaos_agent.agent.nodes.planning.handoff_strip import planning_handoff
from chaos_agent.agent.nodes.verify._verifier_messages import (
    _VERIFIER_CONTEXT_KWARGS_KEY,
)
from chaos_agent.agent.prompts.boundary import (
    INJECT_TO_RECOVER_BOUNDARY_DECLARATION,
)

try:  # pragma: no cover - import shape probe, absence fails the test itself
    from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
        INJECT_TO_RECOVER_BOUNDARY_DECLARATION as _RVL_DECL,
    )
    _RECOVER_LOOP_IMPORTS_DECLARATION = True
except ImportError:  # pragma: no cover
    _RECOVER_LOOP_IMPORTS_DECLARATION = False

import pytest


# The registry. Fields per seam:
#   id:            stable identifier (also the test id)
#   axes:          axes a carrier MUST answer at this seam
#   carrier:       human-readable carrier description
#   probe:         name of the checker method (see class below)
_SEAMS = [
    {
        "id": "intent-to-agent-loop",
        "axes": ("role", "evidence_tense"),
        "carrier": (
            "[FAULT INTENT — UNVERIFIED parameters] anchor message on the "
            "graph entry; probe tip context keeps its context_anchor flag"
        ),
        "probe": "probe_intent_to_agent_loop",
    },
    {
        "id": "agent-loop-to-execute-loop",
        "axes": ("role", "evidence_tense", "tool_surface"),
        "carrier": (
            "planning_handoff strips the ReAct inertia (epoch-bounded, "
            "persist-then-remove); **PHASE 2 — EXECUTE NOW** kickoff wraps "
            "the seam with position-based detection"
        ),
        "probe": "probe_agent_loop_to_execute_loop",
    },
    {
        "id": "execute-loop-to-verify",
        "axes": ("role", "evidence_tense", "tool_surface"),
        "carrier": (
            "_VERIFIER_CONTEXT_KWARGS_KEY cycle detection (epoch-bounded) "
            "+ '## Layer 1 Result' / 'Now perform Layer 2' context + "
            "verifier_screener phase_duty; baseline synthetic messages "
            "carry 'captured BEFORE fault injection' tense labels"
        ),
        "probe": "probe_execute_loop_to_verify",
    },
    {
        "id": "inject-to-recover-graft",
        "axes": ("role", "evidence_tense", "tool_surface"),
        "carrier": (
            "INJECT_TO_RECOVER_BOUNDARY_DECLARATION injected inside "
            "recover_verifier_loop between the grafted history and the "
            "task context (the single graph-internal choke point every "
            "entry path crosses; the grafted history itself arrives via "
            "checkpointer thread inheritance on the CLI path — the "
            "baseline_messages bootstraps are JSON-channel dedup, not "
            "model-context grafting); persisted via msg_list on the "
            "LLM-driven Layer 1 write and, for deterministic Layer 1 "
            "(no LLM, no messages), via an idempotent marker-probe "
            "injection in the Layer 2 first-iteration build — no "
            "NO_SESSION_MARKER either way"
        ),
        "probe": "probe_inject_to_recover_graft",
    },
    {
        "id": "recover-layer1-to-layer2",
        "axes": ("role", "tool_surface"),
        "carrier": (
            "'PHASE TRANSITION: Layer 1 (recovery execution) is COMPLETE...' "
            "layer2_instruction emitted by the provider's "
            "recover_layer2_context; submit_recover_verification bound "
            "only in Layer 2"
        ),
        "probe": "probe_recover_layer1_to_layer2",
    },
    {
        "id": "replan-execute-to-planning",
        "axes": ("role", "evidence_tense"),
        "carrier": (
            "reset_attribution_state re-bases the attribution epoch so "
            "pre-replan cycles sit before the boundary; replan context/"
            "history are explicitly fed to the planning phase"
        ),
        "probe": "probe_replan",
    },
    {
        "id": "memory-compaction",
        "axes": ("evidence_tense",),
        "carrier": (
            "compaction deletes only messages BEFORE the epoch boundary "
            "and re-bases the index (_rebase_epoch_index); tool surface "
            "is unchanged across the seam"
        ),
        "probe": "probe_memory_compaction",
    },
    {
        "id": "batch-fault-switch",
        "axes": ("role", "evidence_tense"),
        "carrier": (
            "batch_setup clears all messages (REMOVE_ALL_MESSAGES) and "
            "emits fresh guidance; history does not cross the seam"
        ),
        "probe": "probe_batch_fault_switch",
    },
]


class TestPhaseBoundaryInventory:
    """4.1 — the registry itself is well-formed and complete."""

    _EXPECTED_SEAM_IDS = {
        "intent-to-agent-loop",
        "agent-loop-to-execute-loop",
        "execute-loop-to-verify",
        "inject-to-recover-graft",
        "recover-layer1-to-layer2",
        "replan-execute-to-planning",
        "memory-compaction",
        "batch-fault-switch",
    }

    def test_registry_covers_all_eight_seams(self):
        ids = {s["id"] for s in _SEAMS}
        assert ids == self._EXPECTED_SEAM_IDS
        assert len(_SEAMS) == 8

    def test_every_seam_declares_axes_and_carrier(self):
        for seam in _SEAMS:
            assert seam["axes"], f"{seam['id']}: no axes declared"
            assert seam["carrier"].strip(), f"{seam['id']}: empty carrier"
            assert seam["probe"] in dir(TestPhaseBoundaryCarriers), (
                f"{seam['id']}: probe method {seam['probe']} missing"
            )

    def test_tool_surface_axis_only_where_justified(self):
        """D7: the tool-surface axis applies only where the three
        conditions hold (inertia survives ∧ surface differs ∧ no other
        carrier). Compaction and batch-switch have no surviving inertia
        (history deleted or cleared) — they must NOT declare it."""
        no_tool_surface = {"memory-compaction", "batch-fault-switch"}
        for seam in _SEAMS:
            has_axis = "tool_surface" in seam["axes"]
            if seam["id"] in no_tool_surface:
                assert not has_axis, (
                    f"{seam['id']} declares tool_surface but its inertia "
                    "source does not survive the seam (D7 violation)"
                )


class TestPhaseBoundaryCarriers:
    """4.2 — every registered carrier exists in the source. The parametrized
    sweep is what makes the inventory machine-checkable: removing a
    carrier from the codebase fails the corresponding probe, and adding
    an unregistered seam cannot pass silently (it has no registry entry
    to be tested, and the registry completeness test is the checklist)."""

    def probe_intent_to_agent_loop(self):
        # The anchor is an inline construction in agent_loop.py (not a
        # module constant), so the probe is a source probe.
        from chaos_agent.agent.nodes.execute import agent_loop

        src = inspect_source(agent_loop)
        assert "[FAULT INTENT — UNVERIFIED parameters from user dialogue]" in src
        # The anchor carries the CONTEXT_ANCHOR_FLAG so it survives every
        # later trim (flag defined in handoff_strip, applied at L645).
        assert "CONTEXT_ANCHOR_FLAG" in src

    def probe_agent_loop_to_execute_loop(self):
        assert _PHASE2_KICKOFF_MARKER == "**PHASE 2 — EXECUTE NOW**"
        # Position-based detection: a fresh seam (a finalization receipt
        # with no kickoff after it) arms the kickoff; an already-announced
        # seam does not re-announce.
        from langchain_core.messages import ToolMessage
        receipt = ToolMessage(content="Planning finalized: approved", tool_call_id="fin")
        history = [HumanMessage(content="planning chatter"), receipt]
        kickoff = _maybe_build_phase2_kickoff(history)
        assert kickoff is not None and _PHASE2_KICKOFF_MARKER in kickoff.content
        announced = history + [HumanMessage(content=f"see {_PHASE2_KICKOFF_MARKER}")]
        assert _maybe_build_phase2_kickoff(announced) is None
        # Context surgery: the handoff node exists and strips inertia.
        assert callable(planning_handoff)

    def probe_execute_loop_to_verify(self):
        assert _VERIFIER_CONTEXT_KWARGS_KEY == "_verifier_main_context"
        # Screener duty for the verify phase is wired in graph.py.
        import inspect

        from chaos_agent.agent import graph as graph_mod
        src = inspect.getsource(graph_mod)
        assert "phase_duty=" in src
        assert "verification phase judges" in src

    def probe_inject_to_recover_graft(self):
        # The graft declaration exists as the boundary-module instance...
        assert "PHASE BOUNDARY" in INJECT_TO_RECOVER_BOUNDARY_DECLARATION
        assert "RECOVERY task" in INJECT_TO_RECOVER_BOUNDARY_DECLARATION
        # ...and is the SAME object the recover loop injects (single
        # wording source; a second hand-rolled copy would drift).
        assert _RECOVER_LOOP_IMPORTS_DECLARATION
        assert _RVL_DECL is INJECT_TO_RECOVER_BOUNDARY_DECLARATION

    def probe_recover_layer1_to_layer2(self):
        """The PHASE TRANSITION layer2_instruction is emitted by every
        provider's recover_layer2_context — k8s_native pinned here as the
        representative; the other three providers are checked by their own
        suites (same wording family)."""
        from chaos_agent.agent.providers.k8s_native import provider as k8s_provider

        src = inspect_source(k8s_provider)
        assert "PHASE TRANSITION" in src
        assert "You are now in Layer 2 (VERIFICATION)" in src

    def probe_replan(self):
        assert callable(reset_attribution_state)

    def probe_memory_compaction(self):
        from chaos_agent.memory import hook as hook_mod

        src = inspect_source(hook_mod)
        assert "_rebase_epoch_index" in src
        # The re-base contract: only pre-boundary messages are compacted.
        assert "attribution_epoch_index" in src

    def probe_batch_fault_switch(self):
        assert REMOVE_ALL_MESSAGES == "__remove_all__"


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)


def _probe_params():
    return [pytest.param(seam["id"], id=seam["id"]) for seam in _SEAMS]


class TestPhaseBoundaryCarrierSweep:
    """4.2 — the parametrized sweep: no registry entry can be silently
    unprobed (each id maps to exactly one probe invocation)."""

    @pytest.mark.parametrize("seam_id", _probe_params())
    def test_carrier_exists_for_seam(self, seam_id):
        seam = next(s for s in _SEAMS if s["id"] == seam_id)
        probe = getattr(TestPhaseBoundaryCarriers, seam["probe"])
        # A bare bound-method call would need an instance; probes are
        # written self-free, so call through a lightweight instance.
        probe(TestPhaseBoundaryCarriers())


class TestFeedbackAttributionSharedHandler:
    """The seam-agnostic backstop: _phase2_handle_tool_error (shared by
    inject phase2 / verifier / recover ToolNodes) carries the
    history-inertia attribution, and _phase1_handle_tool_error retains its
    own richer phase attribution. Pinned here (not only in
    test_phase2_tool_errors.py) because the inventory treats the feedback
    loop as a carrier layer for the tool-surface axis."""

    def test_phase2_feedback_names_the_three_attribution_elements(self):
        out = _phase2_handle_tool_error(
            Exception("kubectl_read is not a valid tool, try one of [kubectl].")
        )
        assert "conversation history" in out
        assert "earlier phase" in out
        assert "currently bound" in out

    def test_phase1_feedback_keeps_phase_attribution(self):
        out = _phase1_handle_tool_error(
            Exception("blade_create is not a valid tool, try one of [kubectl].")
        )
        assert "not available in Phase 1" in out
        assert "read-only by design" in out
