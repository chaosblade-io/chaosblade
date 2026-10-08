"""Graph-level terminal-state contract for the fault-window hold origin.

Provenance (adversarial review, 2026-09-19; re-anchored by
hold-reanchor-recovery-grace, 2026-09-22): the hold tests in
tests/test_server/routes/test_turn_hold_fault_window.py feed a
hand-built ``SnapshotPipelineGraph`` terminal state — the REAL inject
pipeline's post-verifier node chain (``terminal_reports`` →
``save_memory``, graph.py) is never exercised there. A future
"privacy / state-reset" write clearing the attribution fields in
either store node would flip the hold into silent switch-off while
every one of those unit tests stays green.

This module puts teeth on exactly that gap: it chains the REAL nodes
(``verifier`` simple entry → ``terminal_reports`` → ``save_memory``)
inside a real ``StateGraph(AgentState)`` round-trip — the same defense
pattern as test_recover_verifier.py's B51 pin — and asserts the
TERMINAL state still carries the window origin the hold reads:

  - ``injection_start_time`` — the ISSUED stamp, the hold's window
    origin AND the same origin the fault's own recovery timer counts
    from (input here; must survive the chain byte-identical);
  - the retired ``injection_window_start_time`` (verifier-entry
    origin) must NOT reappear: the verifier no longer stamps any
    window field, and a stale re-introduction would silently shift
    the hold's dispatch off the issued anchor.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from chaos_agent.utils.time import now_iso, parse_iso_timestamp


def _mock_blade_running(uid: str = "abc123xyz"):
    """Mock execute_via_transport to return a Running blade_status —
    the same transport seam and payload shape test_verifier.py uses to
    drive the simple verifier entry to a passed Layer 1."""
    from chaos_agent.tools.shell import CommandResult

    return AsyncMock(return_value=CommandResult(
        exit_code=0,
        stdout=json.dumps({
            "code": 200, "success": True,
            "result": {"Uid": uid, "Status": "Running"},
        }),
        stderr="",
    ))


async def test_terminal_state_keeps_window_fields_through_real_node_chain(monkeypatch):
    """The hold's window inputs must exist in the state a REAL pipeline
    leaves at END — not just in the snapshot dict the hold unit tests
    hand-build. Fails loudly on: a clearing write in terminal_reports /
    save_memory, a verifier that stops stamping, or a stamp that moves
    off the top-level channel."""
    from langgraph.graph import END, START, StateGraph

    from chaos_agent.agent.nodes.store import memory_nodes
    from chaos_agent.agent.nodes.store import terminal_reports as tr_mod
    from chaos_agent.agent.nodes.store.memory_nodes import save_memory
    from chaos_agent.agent.nodes.store.terminal_reports import terminal_reports_node
    from chaos_agent.agent.nodes.verify.verifier import verifier
    from chaos_agent.agent.state import AgentState
    from chaos_agent.config import settings as s_mod

    # Store-node machinery: the state CONTRACT is under test, not the
    # persistence layer — no-op the side channels both nodes touch.
    monkeypatch.setattr(tr_mod, "sync_node_status_to_session", lambda *a, **k: None)
    monkeypatch.setattr(memory_nodes, "sync_node_status_to_session", lambda *a, **k: None)
    monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
    monkeypatch.setattr(memory_nodes, "_finalize_session_store", AsyncMock())
    # Postmortem / self-evolution are orthogonal LLM side channels
    # (covered by test_terminal_reports.py); issue-report publishing is
    # gated on github_token, empty in tests.
    monkeypatch.setattr(s_mod.settings, "postmortem_enabled", False)
    monkeypatch.setattr(s_mod.settings, "self_evolution", False)

    # The post-verifier segment of graph.py's inject pipeline, real
    # node functions and real edge order (graph.py wires exactly
    # verifier_loop → terminal_reports → save_memory → END).
    graph = StateGraph(AgentState)
    graph.add_node("verifier_loop", verifier)
    graph.add_node("terminal_reports", terminal_reports_node)
    graph.add_node("save_memory", save_memory)
    graph.add_edge(START, "verifier_loop")
    graph.add_edge("verifier_loop", "terminal_reports")
    graph.add_edge("terminal_reports", "save_memory")
    graph.add_edge("save_memory", END)

    # The "execute-loop just concluded" input shape: the issued stamp
    # is already in place — nothing downstream may re-anchor or clear
    # it (that stamp IS the hold's window origin now).
    start_iso = now_iso()
    initial = {
        "task_id": "inject-20260919-120000-term01",
        "confirmed_intent": "inject",
        "skill_name": "pod-delete",
        "experiment_uid": "abc123xyz",
        "injection_start_time": start_iso,
        "fault_spec": {"duration_seconds": 300},
        "messages": [],
    }

    with patch(
        "chaos_agent.agent.providers.chaosblade.cli.execute_via_transport",
        _mock_blade_running(),
    ):
        out = await graph.compile().ainvoke(initial)

    # Non-vacuity guards: all three nodes actually ran their inject
    # paths (an early-exit anywhere above would make the survival
    # assertions below meaningless).
    assert out.get("verification"), "verifier did not write its envelope"
    assert out.get("finished_at"), "save_memory did not complete"
    assert "postmortem" in out, "terminal_reports did not write its R11 keys"

    # THE contract: the hold's window origin lives in the terminal state.
    assert out.get("injection_start_time") == start_iso, (
        "the issued stamp (hold window origin) must survive the node chain "
        "byte-identical — a clearing write in terminal_reports/save_memory "
        "silently degrades the fault-window hold to switch-off"
    )
    # The hold parses exactly this value and silently no-ops on an
    # unparseable one (the unparseable guard), so parse it HERE too.
    assert parse_iso_timestamp(out.get("injection_start_time")) is not None
    # The retired verifier-entry origin must not reappear: nothing in
    # the chain stamps a window field anymore.
    assert not out.get("injection_window_start_time"), (
        "injection_window_start_time is retired (hold-reanchor-recovery-"
        "grace) — a node re-stamping it would drift the contract"
    )
